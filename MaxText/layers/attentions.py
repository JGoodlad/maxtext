#  Copyright 2023 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Attentions Layers."""

import functools
import math
from typing import Optional, Sequence

from flax import linen as nn
import jax
from jax import lax
from jax import random
from jax.ad_checkpoint import checkpoint_name
from jax.experimental import shard_map
from jax.experimental.pallas.ops.tpu.splash_attention import splash_attention_mask
from jax.experimental.pallas.ops.tpu.splash_attention import splash_attention_kernel
from kernels.ragged_attention import mqa_reference, ragged_mqa
from jax.sharding import PartitionSpec as P
import jax.numpy as jnp

import common_types
from layers import embeddings
from layers import initializers
from layers import linears
from layers import quantizations


Array = common_types.Array
Config = common_types.Config
DType = common_types.DType
Mesh = common_types.Mesh
PRNGKey = common_types.PRNGKey

DenseGeneral = linears.DenseGeneral
RotaryEmbedding = embeddings.RotaryEmbedding
NdInitializer = initializers.NdInitializer
Quant = quantizations.AqtQuantization

AxisNames = common_types.AxisNames
AxisIdxes = common_types.AxisIdxes
BATCH = common_types.BATCH
LENGTH = common_types.LENGTH
HEAD = common_types.HEAD
D_KV = common_types.D_KV
CACHE_BATCH = common_types.CACHE_BATCH
CACHE_SEQUENCE = common_types.CACHE_SEQUENCE
CACHE_HEADS = common_types.CACHE_HEADS
CACHE_KV = common_types.CACHE_KV
DEFAULT_MASK_VALUE = -0.7 * float(jnp.finfo(jnp.dtype("float32")).max)


nd_dense_init = initializers.nd_dense_init
shard_map = shard_map.shard_map

dynamic_vector_slice_in_dim = jax.vmap(lax.dynamic_slice_in_dim, in_axes=(None, 0, None, None))

# pylint: disable=line-too-long, g-doc-args, g-doc-return-or-yield, bad-continuation, g-inconsistent-quotes
# pytype: disable=attribute-error


def validate_compute_axis_order(s: str) -> None:
  valid_compute_axis_order = ((0,1,2,3), (0,2,1,3))
  if s not in valid_compute_axis_order:  # currently supported compute_axis_order
    raise ValueError("Invalid compute_axis_order was passed. Valid options ", valid_compute_axis_order)


def apply_mask_to_logits(logits: Array, mask: Array):
  """Applies a floating-point mask to a set of logits.

  The mask is represented as a tensor with some dtype where 0 represents true and values
  below a large negative number (here set to
  get_large_negative_number(logits.dtype) / 2) represent false. Applying the mask
  leaves the logits alone in the true case and replaces them by
  get_large_negative_number(logits.dtype) in the false case. Previously, this was
  done by adding the logits to the mask; however, this leads to a bad fusion
  decision in the compiler that saves the values in memory rather than
  just the predicate. This implementation avoids that problem.

  from https://github.com/google/praxis/blob/4712a6b9ee13e224b86e235ff55f7c6bab9fbab3/praxis/py_utils.py#L706

  Args:
    logits: A JTensor of logit values.
    mask: A JTensor of mask values with the encoding described in the
      function documentation.

  Returns:
    Masked logits.
  """
  return jnp.where((mask >= DEFAULT_MASK_VALUE * 0.5), logits, DEFAULT_MASK_VALUE)


def _maybe_aqt_einsum(quant: Quant):
  """Maybe overwrite dot general with aqt_dot_general."""
  return jnp.einsum if quant is None else quant.einsum()


class AttentionOp(nn.Module):
  mesh: Mesh
  attention_kernel: str
  max_target_length: int
  num_query_heads: int
  num_kv_heads: int
  float32_qk_product: bool = False
  max_prefill_predict_length: int = -1
  float32_logits: bool = False
  flash_axis_names: AxisNames = (BATCH, HEAD, LENGTH, D_KV)
  ragged_qkv_axis_names: AxisNames = (CACHE_BATCH, CACHE_HEADS, CACHE_SEQUENCE, CACHE_KV)
  ragged_lengths_names: AxisNames = (CACHE_BATCH,)
  # kv_cache_logical_layout: AxisNames = (CACHE_BATCH, CACHE_SEQUENCE, CACHE_HEADS, CACHE_KV)
  kv_cache_logical_layout: AxisNames = (CACHE_HEADS, CACHE_BATCH, CACHE_SEQUENCE, CACHE_KV)
  prefill_cache_axis_order: AxisIdxes = (0, 1, 2, 3)
  ar_cache_axis_order: AxisIdxes = (0, 1, 2, 3)
  compute_axis_order: AxisIdxes = (0, 1, 2, 3)
  reshape_q: bool = False
  dropout_rate: float = 0.0
  dtype: DType = jnp.float32
  quant: Optional[Quant] = None
  quantize_kvcache: bool = False

  def check_attention_inputs(self, query: Array, key: Array, value: Array) -> None:
    """Check attention inputs."""

    assert key.ndim == value.ndim, "k, v must have same rank."
    assert query.shape[:-3] == key.shape[:-3] == value.shape[:-3], "q, k, v batch dims must match."
    assert key.shape[-2] == value.shape[-2], "k, v num_kv_heads must match."
    assert key.shape[-3] == value.shape[-3], "k, v lengths must match."
    assert query.shape[-1] == key.shape[-1], "q, k depths must match."

  # Following Pallas MHA Flash Attention Reference.
  # https://github.com/google/jax/blob/main/jax/experimental/pallas/ops/tpu/flash_attention.py
  # This mask models (1) separate sequences (decoder_segment_ids) and (2) causality
  def generate_attention_mask(self, query, key, decoder_segment_ids: Array | None, model_mode: str) -> Array | None:
    mask = None
    if model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE:
      mask = decoder_segment_ids[None, :, None, None, :] == common_types.DECODING_ACTIVE_SEQUENCE_INDICATOR
    elif decoder_segment_ids is not None:
      mask = decoder_segment_ids[:, :, None] == decoder_segment_ids[:, None, :]
      mask = mask[None, :, None, :, :]

    print(f"generate_attention_mask - {query.shape=}")
    print(f"generate_attention_mask - {key.shape=}")
    # generate_attention_mask - query.shape=(4, 32, 1024, 128)
    # generate_attention_mask - key.shape=(4, 32, 1024, 128)
    if decoder_segment_ids is not None:
      print(f"generate_attention_mask - {decoder_segment_ids.shape=}")
      # generate_attention_mask - decoder_segment_ids.shape=(4, 1024)
    else: 
      print(f"generate_attention_mask - decoder_segment_ids=None")

    if mask is not None:
      print(f"generate_attention_mask - {mask.shape=}")
      # generate_attention_mask - mask.shape=(4, 1, 1, 1024, 1024)
    else:
      print(f"generate_attention_mask - mask=None")
    causal_mask = None
    # We enforce causality except for AUTOREGRESSION
    if model_mode != common_types.MODEL_MODE_AUTOREGRESSIVE:
      _, _, q_seq_len, _ = query.shape
      _, _, kv_seq_len, _ = key.shape
      mask_shape = (q_seq_len, kv_seq_len)
      row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
      col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
      causal_mask = (col_ids <= row_ids)[None, None, None, :, :]

    if (mask is not None) and (causal_mask is not None):
      output_mask = jnp.logical_and(mask, causal_mask)
    elif mask is not None:
      output_mask = mask
    elif causal_mask is not None:
      output_mask = causal_mask
    else:
      output_mask = None

    result = jnp.where(output_mask, 0.0, DEFAULT_MASK_VALUE) if output_mask is not None else None
    print(f"generate_attention_mask - {result.shape=}")
    # generate_attention_mask - mask.shape=(4, 1, 1, 1024, 1024)
    return result


  def apply_attention(self, query: Array, key: Array, value: Array, decoder_segment_ids: Array | None, lengths: Array | None, model_mode: str, use_ragged: str = False):
    self.check_attention_inputs(query, key, value)
    length = query.shape[-3]
    if use_ragged and model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE:
      if lengths is None:
        lengths = jnp.sum(decoder_segment_ids, axis=-1)
      return self.ragged_attention(query, key, value, lengths)
    # elif (
    #     self.attention_kernel == "dot_product"
    #     or (self.attention_kernel == "autoselected" and model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE)
    #     or (self.attention_kernel == "autoselected" and length < 128)
    # ):
    else:
      return self.apply_attention_dot(query, key, value, decoder_segment_ids, model_mode)
    # elif self.attention_kernel == "flash" or self.attention_kernel == "autoselected":
    #   if model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE:
    #     raise ValueError(
    #         """Decode not supported with flash attention.
    #                         Use `dot_product` instead."""
    #     )
    #   return self.tpu_flash_attention(query, key, value, decoder_segment_ids), None, None
    # elif self.attention_kernel == "cudnn_flash_te":
    #   if model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE:
    #     raise ValueError(
    #         """Decode not supported with flash attention.
    #                        Use `dot_product` instead."""
    #     )
    #   return self.cudnn_flash_attention(query, key, value, decoder_segment_ids, model_mode), None, None
    # else:
    #   raise ValueError(f"Unexpected attention kernel {self.attention_kernel=}.")


  def mqa_attention_ref(self, query: Array, key: Array, value: Array, lengths: Array) -> tuple[Array, Array, Array]:
    query = jnp.swapaxes(query, 1, 2)
    key = jnp.swapaxes(key, 1, 2)
    value = jnp.swapaxes(value, 1, 2)
    vmap_mqa_ref = jax.vmap(mqa_reference, in_axes=[1, 1, 1, None], out_axes=2)
    o, m, l  = vmap_mqa_ref(query, key, value, lengths)
    m = jnp.expand_dims(m, axis=-1)
    l = jnp.expand_dims(l, axis=-1)
    o = o * l 
    return o, m, l
  
  def ragged_attention(self, query: Array, key: Array, value: Array, lengths: Array) -> tuple[Array, Array, Array]:
    """Ragged Attention."""
    # jax.debug.print("lengths: {}", lengths)
    # ragged_attention - query.shape=(32, 1, 32, 128)
    # ragged_attention - key.shape=(32, 32, 1024, 128)
    # ragged_attention - value.shape=(32, 32, 1024, 128)
    # query = jnp.transpose(query, axes=(0, 2, 1, 3))
    print(f"ragged_attention - {query.shape=}")
    print(f"ragged_attention - {key.shape=}")
    print(f"ragged_attention - {value.shape=}")
    print(f"ragged_attention - {lengths.shape=}")
    # key = jnp.transpose(key, axes=(0, 2, 1, 3))
    # value = jnp.transpose(value, axes=(0, 2, 1, 3))
    # query.shape=(4, 32, 1, 128)
    # key.shape=(4, 32, 1024, 128)
    # value.shape=(4, 32, 1024, 128)

    ragged_qkv = nn.logical_to_mesh_axes(self.ragged_qkv_axis_names)
    ragged_lengths = nn.logical_to_mesh_axes(self.ragged_lengths_names)
    ragged_output = nn.logical_to_mesh_axes(self.kv_cache_logical_layout)
    @functools.partial(
        shard_map,
        mesh=self.mesh,
        in_specs=(
            P('tensor', None, None, None),
            P('tensor', None, None, None),
            P('tensor', None, None, None),
            P(None),
        ),
        out_specs=P('tensor', None, None, None),
        check_rep=False,
    )
    def wrap_ragged_attention(query, key, value, lengths):
      vmap_ragged_mqa = jax.vmap(ragged_mqa, in_axes=[0, 0, 0, None], out_axes=0)
      o, m, l  = vmap_ragged_mqa(query, key, value, lengths)
      m = jnp.expand_dims(m, axis=-1)
      l = jnp.expand_dims(l, axis=-1)
      o = o * l 
      return o, m, l
   
    print()
    print(f"calling wrap_ragged_attention - {query.shape=}")
    print(f"calling wrap_ragged_attention - {key.shape=}")
    print(f"calling wrap_ragged_attention - {value.shape=}")
    print(f"calling wrap_ragged_attention - {lengths.shape=}")
    # calling wrap_ragged_attention - query.shape=(32, 1, 32, 128)
    # calling wrap_ragged_attention - key.shape=(32, 1024, 32, 128)
    # calling wrap_ragged_attention - value.shape=(32, 1024, 32, 128)
    o, m, l = wrap_ragged_attention(query, key, value, lengths)
    print(f"calling wrap_ragged_attention - {o.shape=}")
    print(f"calling wrap_ragged_attention - {m.shape=}")
    print(f"calling wrap_ragged_attention - {l.shape=}")
    # o = jnp.swapaxes(o, 1, 2) 
    # m = jnp.swapaxes(m, 1, 2)
    # l = jnp.swapaxes(l, 1, 2)
    return o, m, l


  def tpu_flash_attention(self, query: Array, key: Array, value: Array, decoder_segment_ids: Array | None) -> Array:
    """TPU Flash Attention."""
    # Transpose to ('batch', 'heads', 'length', 'kv')
    query = jnp.transpose(query, axes=(0, 2, 1, 3))
    key = jnp.transpose(key, axes=(0, 2, 1, 3))
    value = jnp.transpose(value, axes=(0, 2, 1, 3))

    if decoder_segment_ids is not None:
      decoder_segment_ids = splash_attention_kernel.SegmentIds(decoder_segment_ids, decoder_segment_ids)
    axis_names = nn.logical_to_mesh_axes(self.flash_axis_names)
    segment_axis_names = nn.logical_to_mesh_axes((BATCH, "activation_length_no_heads"))

    @functools.partial(
        shard_map,
        mesh=self.mesh,
        in_specs=(
            axis_names,
            axis_names,
            axis_names,
            segment_axis_names,
        ),
        out_specs=axis_names,
        check_rep=False,
    )
    def wrap_flash_attention(query, key, value, decoder_segment_ids):
      if decoder_segment_ids is not None:
        assert (
            query.shape[2] == decoder_segment_ids.q.shape[1]
        ), "Sharding along sequence dimension not allowed in tpu kernel attention"
      block_sizes = splash_attention_kernel.BlockSizes(
          block_q=min(512, query.shape[2]),
          block_kv_compute=min(512, key.shape[2]),
          block_kv=min(512, key.shape[2]),
          block_q_dkv=min(512, query.shape[2]),
          block_kv_dkv=min(512, key.shape[2]),
          block_kv_dkv_compute=min(512, query.shape[2]),
          block_q_dq=min(512, query.shape[2]),
          block_kv_dq=min(512, query.shape[2]),
      )

      masks = [splash_attention_mask.CausalMask(shape=(query.shape[2], query.shape[2])) for i in range(query.shape[1])]
      multi_head_mask = splash_attention_mask.MultiHeadMask(masks=masks)
      splash_kernel = splash_attention_kernel.make_splash_mha(
          mask=multi_head_mask, head_shards=1, q_seq_shards=1, block_sizes=block_sizes
      )

      return jax.vmap(splash_kernel)(query, key, value, segment_ids=decoder_segment_ids)

    devices_in_data_fsdp = self.mesh.shape["data"] * self.mesh.shape["fsdp"]
    assert (query.shape[0] / devices_in_data_fsdp).is_integer(), (
        "Batch dimension should be shardable among the devices in data and fsdp" " axis"
    )
    x = wrap_flash_attention(query, key, value, decoder_segment_ids)
    x = jnp.transpose(x, axes=(0, 2, 1, 3))
    return x

  def cudnn_flash_attention(
      self,
      query: Array,
      key: Array,
      value: Array,
      decoder_segment_ids: Array | None,
      model_mode: str = common_types.MODEL_MODE_TRAIN,
  ) -> Array:
    """CUDNN Flash Attention with Transformer Engine.
    1. Stable API, supports GQA
    2. Supports head_dim till 128; head_dim=256 support will be added soon
    """
    # These imports are only meant to work in a GPU build.
    from transformer_engine.jax.flax.transformer import DotProductAttention  # pytype: disable=import-error

    _, _, _, head_dim = query.shape  # pylint: disable=unused-variable

    # generate attn_mask
    attn_mask = self.generate_attention_mask(query, key, decoder_segment_ids, model_mode)

    dpa_layer = DotProductAttention(
        head_dim=head_dim,
        num_attention_heads=self.num_query_heads,
        num_gqa_groups=self.num_kv_heads,
        attn_mask_type="causal",  # 'causal' or 'padding'
        attn_bias_type="NO_BIAS",  # 'no_bias', 'pre_scale_bias' or 'post_scale_bias'
        attention_dropout=self.dropout_rate,
        dropout_rng_name="aqt",
        dtype=self.dtype,
        float32_logits=self.float32_logits,
        qkv_layout="BSHD_BSHD_BSHD",  # 'BS3HD', 'BSHD_BS2HD' or 'BSHD_BSHD_BSHD'
        scale_factor=1.0 / math.sqrt(head_dim),
        transpose_batch_sequence=False,
    )
    return dpa_layer(query, key, value, mask=attn_mask)

  def compute_local_attention(self, attn_weights: Array, value: Array, q_seq_len: int, model_mode: str) -> tuple[Array, Array, Array]:
    """Computes the attention of a local subset of the kv cache.
    Local attention results will need to be combined with any other local attentions and normalized
    Based on https://github.com/google-research/google-research/blob/master/scaling_transformer_inference_efficiency/attention.py

    Args:
        attn_weights (Array): Product of query and key
        value (Array): Current value
        aqt_rng (PRNGKey | None): Optional rng

    Returns:
        (local_out, local_max,): where
          local_out is local unnormalized output
          local_max is the local max of exponentials
          local_sum is the sum of exponentials for this chunk, divided by exp(local_max).
    """
    local_max = jnp.max(attn_weights, axis=-1, keepdims=True)
    local_exps = jnp.exp(attn_weights - local_max)
    local_sum = jnp.sum(local_exps, axis=-1, keepdims=True)

    print(f"compute_local_attention - initial {attn_weights.shape=}")
    print(f"compute_local_attention - initial {value.shape=}")
    print(f"compute_local_attention - initial {q_seq_len=}")
    # compute_local_attention - initial attn_weights.shape=(4, 32, 1, 2048, 2048)
    # compute_local_attention - initial value.shape=(4, 2048, 32, 128)
    # compute_local_attention - initial q_seq_len=2048

    print(f"compute_local_attention - initial {local_max.shape=}")
    print(f"compute_local_attention - initial {local_sum.shape=}")
    print(f"compute_local_attention - initial {local_exps.shape=}")
    # compute_local_attention - initial local_max.shape=(4, 32, 1, 2048, 1)
    # compute_local_attention - initial local_sum.shape=(4, 32, 1, 2048, 1)
    # compute_local_attention - initial local_exps.shape=(4, 32, 1, 2048, 2048)

    local_sum = jnp.moveaxis(local_sum, -2, 2)
    local_max = jnp.moveaxis(local_max, -2, 2)
    print(f"compute_local_attention - moveaxis {local_max.shape=}")
    print(f"compute_local_attention - moveaxis {local_sum.shape=}")
    # compute_local_attention - moveaxis local_max.shape=(4, 2048, 32, 1, 1)
    # compute_local_attention - moveaxis local_sum.shape=(4, 2048, 32, 1, 1)

    local_max = jnp.reshape(local_max, (local_max.shape[0], local_max.shape[1], local_max.shape[2] * local_max.shape[3], 1))
    local_sum = jnp.reshape(local_sum, (local_sum.shape[0], local_sum.shape[1], local_sum.shape[2] * local_sum.shape[3], 1))
    print(f"compute_local_attention - reshape {local_max.shape=}")
    print(f"compute_local_attention - reshape {local_sum.shape=}")
    # compute_local_attention - reshape local_max.shape=(4, 32, 2048, 1)
    # compute_local_attention - reshape local_sum.shape=(4, 32, 2048, 1)

    local_out = self.wv_product(local_exps, value, model_mode)

    if self.reshape_q and q_seq_len == 1:
      local_max = local_max[:,0:1,:,:]
      local_sum = local_sum[:,0:1,:,:]
      local_out = local_out[:,0:1,:,:]

    print(f"compute_local_attention - final {local_max.shape=}")
    print(f"compute_local_attention - final {local_exps.shape=}")
    print(f"compute_local_attention - final {local_sum.shape=}")
    print(f"compute_local_attention - final {local_out.shape=}")
    # compute_local_attention - final local_max.shape=(4, 32, 2048, 1)
    # compute_local_attention - final local_exps.shape=(4, 32, 1, 2048, 2048)
    # compute_local_attention - final local_sum.shape=(4, 32, 2048, 1)
    # compute_local_attention - final local_out.shape=(4, 32, 2048, 128)
    return local_out, local_max, local_sum

  def apply_attention_dot(
      self,
      query: Array,
      key: Array,
      value: Array,
      decoder_segment_ids: Array | None,
      model_mode: str = common_types.MODEL_MODE_TRAIN,
  ):
    """Apply Attention."""
    print(f"apply_attention_dot - {query.shape=}")
    print(f"apply_attention_dot - {key.shape=}")
    print(f"apply_attention_dot - {value.shape=}")
    # apply_attention_dot - query.shape=(48, 32, 2048, 128)
    # apply_attention_dot - key.shape=(48, 32, 2048, 128)
    # apply_attention_dot - value.shape=(48, 32, 2048, 128)
    # query = jnp.swapaxes(query, 1, 2)
    # key = jnp.swapaxes(key, 1, 2)
    # value = jnp.swapaxes(value, 1, 2)
    if decoder_segment_ids is not None:
      print(f"apply_attention_dot - {decoder_segment_ids.shape=}")
    validate_compute_axis_order(self.compute_axis_order)
    # Casting qk_product and softmaxt computation for float32 for model stability.
    if model_mode == common_types.MODEL_MODE_TRAIN and self.float32_qk_product:
      query = query.astype(jnp.float32)
      key = key.astype(jnp.float32)

    q_seq_len = query.shape[2]
    print(f"apply_attention_dot - {q_seq_len=}")
    attn_weights = self.qk_product(query, key, q_seq_len, model_mode)

    print(f"apply_attention_dot - {attn_weights.shape=}")
    # Casting softmaxt computation for float32 for model stability.
    if model_mode == common_types.MODEL_MODE_TRAIN and self.float32_logits:
      attn_weights = attn_weights.astype(jnp.float32)
    attn_mask = self.generate_attention_mask(query, key, decoder_segment_ids, model_mode)
    print(f"apply_attention_dot - {attn_mask.shape=}")
    if attn_mask is not None:
      attn_weights = apply_mask_to_logits(attn_weights, attn_mask)
    return self.compute_local_attention(attn_weights, value, q_seq_len, model_mode)

  def qk_product(self, query: Array, key: Array, q_seq_len: int, model_mode: str) -> Array:
    """Query-Key product.

    Args:
      query: Query projection, in shape of [b, t, n, d]
      key: Key projection in shape of [b, s, n_kv, d]

    Returns:
      results in shape [b, n_kv, n // n_kv, t, s].

    Annotations:
      b: batch size
      t: query length
      s: key / value length
      d: head / kv dimension
      n: number of query heads
      n_kv: number of kv heads, sometimes annotated as k
      n // n_kv: number of group for query, sometimes annotated with g
    """
    print(f"\nqk_product - {query.shape=}")
    print(f"qk_product - {key.shape=}")
    print(f"qk_product - {q_seq_len=}")
    n, b, t, d = query.shape
    n_kv = key.shape[0]
    assert n_kv == self.num_kv_heads
    # if model_mode == common_types.MODEL_MODE_TRAIN or self.compute_axis_order == (0,1,2,3):
    #   query = jnp.reshape(query, (b, t, n_kv, n // n_kv, d))
    #   if self.reshape_q and q_seq_len == 1:
    #     query = jnp.broadcast_to(query, (b, 2, n_kv, n // n_kv, d))
    #   result = jnp.einsum("btkgd,bskd->bkgts", query, key)
    # elif self.compute_axis_order == (0,2,1,3):
    # query = jnp.transpose(query, axes=self.compute_axis_order)
    # key = jnp.transpose(key, axes=self.compute_axis_order)
    # query = jnp.reshape(query, (b, n_kv, n // n_kv, t, d))
    query = jnp.reshape(query, (n_kv, b, n // n_kv, t, d))
    
    print(f"qk_product - reshape {query.shape=}")
    result = jnp.einsum("kbgtd,kbsd->kbgts", query, key)
    print(f"qk_product - {result.shape=}")
    return result

  def wv_product(self, attn_weights: Array, value: Array, model_mode: str) -> Array:
    """weighted value product.

    Args:
      attn_weights: Computed results of qk_einsum, in shape [b, n_kv, n // n_kv, t, s]
      value: Value projection, in shape of [b, s, n_kv, d]

    Returns:
      result in shape [b, t, n, d]

    Annotations:
      b: batch size
      t: query length
      s: key / value length
      d: head / kv dimension
      n: number of query heads
      n_kv: number of kv heads, sometimes annotated as k
      n // n_kv: number of group for query, sometimes annotated with g
    """
    print(f"\nwv_product - {attn_weights.shape=}")
    print(f"wv_product - {value.shape=}")
    # wv_product - attn_weights.shape=(4, 32, 1, 2048, 2048)
    # wv_product - value.shape=(4, 32, 2048, 128)


    # if model_mode == common_types.MODEL_MODE_TRAIN or self.compute_axis_order == (0,1,2,3):
    #   out = jnp.einsum("bkgts,bskd->btkgd", attn_weights, value)
    #   b, t, n_kv, g, d = out.shape
    #   result = jnp.reshape(out, (b, t, n_kv * g, d))
    # elif self.compute_axis_order == (0,2,1,3):
    # value = jnp.transpose(value, axes=self.compute_axis_order)
    out = jnp.einsum("kbgts,kbsd->kbgtd", attn_weights, value)
    n_kv, b, g, t, d = out.shape

    print(f"wv_product - {out.shape=}")
    # wv_product - out.shape=(4, 32, 1, 2048, 128)

    result = jnp.reshape(out, (n_kv * g, b, t, d))
    print(f"wv_product - reshape {result.shape=}")
    # wv_product - reshape result.shape=(4, 32, 2048, 128)

    # result = jnp.transpose(result, axes=self.compute_axis_order)
    # print(f"wv_product - transpose {result.shape=}")
    return result

  def revert_kv_cache(self, kv, cached_axis_order):
    """Revert key/value cache to logical shape.

    Args:
      kv: reshaped kv as defined in cached_axis_order

    Returns:
      revert kv to logical shape as [b, s, n_kv, d]

    Annotations:
      b: batch size
      s: key / value length
      n_kv: number of kv heads, sometimes annotated as k
      d: head / kv dimension

    """
    return jax.numpy.moveaxis(kv, (0, 1, 2, 3), cached_axis_order)

  def reshape_kv_cache(self, kv, cached_axis_order):
    """Reshape key/value cache as defined in cached_axis_order.

    Args:
      kv: in logical shape as [b, s, n_kv, d]

    Returns:
      reshaped kv as defined in cached_axis_order

    Annotations:
      b: batch size
      s: key / value length
      n_kv: number of kv heads, sometimes annotated as k
      d: head / kv dimension

    """
    axis_order_to_index_mapping = {a:i for i, a in enumerate(cached_axis_order)}
    axis_destination = tuple([i for a, i in sorted(axis_order_to_index_mapping.items())])
    print(f"reshape_kv_cache - {axis_destination=}")
    return jax.numpy.moveaxis(kv, (0, 1, 2, 3), axis_destination)

  def cached_kv_layout(self, kv_layout, cached_axis_order):
    return tuple([kv_layout[i] for i in cached_axis_order])

  def cached_kv_shape(self, kv_shape, cached_axis_order):
    """Cached KV shape.

    The key and value have dimension [b, s, n_kv, d], but
    we cache them as defined in cached_axis_order for optimized read/write performance.

    Args:
      kv_shape: shape of key or value for caching, as [b, s, n_kv, d].

    Returns:
      Swapped kv_shape as defined in cached_axis_order for cache.

    Annotations:
      b: batch size
      s: key / value length
      n_kv: number of kv heads, sometimes annotated as k
      d: head / kv dimension

    """
    return tuple([kv_shape[i] for i in cached_axis_order])

  def _get_prefill_cache(self, batch, heads, kv_head_size, quantize_kvcache):
    dtype = jnp.int8 if quantize_kvcache else jnp.bfloat16

    # cache_logical_shape = (batch, self.max_prefill_predict_length, heads, kv_head_size)
    # cache_logical_shape = (batch, heads, self.max_prefill_predict_length, kv_head_size)
    cache_logical_shape = (heads, batch, self.max_prefill_predict_length, kv_head_size)

    key_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.prefill_cache_axis_order)
    value_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.prefill_cache_axis_order)

    key_shape = self.cached_kv_shape(cache_logical_shape, self.prefill_cache_axis_order)
    value_shape = self.cached_kv_shape(cache_logical_shape, self.prefill_cache_axis_order)

    print(f"_get_prefill_cache - {key_layout=}")
    print(f"_get_prefill_cache - {value_layout=}")
    print(f"_get_prefill_cache - {key_shape=}")
    print(f"_get_prefill_cache - {value_shape=}")

    cached_key = self.variable(
        "cache",
        "cached_prefill_key",
        nn.with_logical_partitioning(jnp.zeros, key_layout),
        key_shape,
        dtype,
    )
    cached_value = self.variable(
        "cache",
        "cached_prefill_value",
        nn.with_logical_partitioning(jnp.zeros, value_layout),
        value_shape,
        dtype,
    )
    cached_segment_id = self.variable(
        "cache",
        "cache_prefill_segment_id",
        nn.with_logical_partitioning(jnp.zeros, (CACHE_BATCH, CACHE_SEQUENCE)),
        (cache_logical_shape[0], self.max_prefill_predict_length),
        jnp.int32,
    )

    if self.quantize_kvcache:

      cache_logical_shape_scale = (batch, self.max_prefill_predict_length, heads, 1)

      key_shape_scale = self.cached_kv_shape(cache_logical_shape_scale, self.prefill_cache_axis_order)
      value_shape_scale = self.cached_kv_shape(cache_logical_shape_scale, self.prefill_cache_axis_order)

      cached_key_scale_var = self.variable(
          "cache",
          "cached_prefill_key_scale",
          nn.with_logical_partitioning(jnp.zeros, key_layout),
          key_shape_scale,
          jnp.bfloat16,
      )
      cached_value_scale_var = self.variable(
          "cache",
          "cached_prefill_value_scale",
          nn.with_logical_partitioning(jnp.zeros, value_layout),
          value_shape_scale,
          jnp.bfloat16,
      )
    else:
      cached_key_scale_var = None
      cached_value_scale_var = None

    key_vars = (cached_key, cached_key_scale_var)
    value_vars = (cached_value, cached_value_scale_var)
    return key_vars, value_vars, cached_segment_id

  def get_ar_layouts(self):
    # self.ar_cache_axis_order=(0, 2, 1, 3)
    # self.kv_cache_logical_layout=('cache_batch', 'cache_sequence', 'cache_heads', 'cache_kv')
    key_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.ar_cache_axis_order)
    value_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.ar_cache_axis_order)
    print()
    print(f"get_ar_layouts - {key_layout=}")
    print(f"get_ar_layouts - {value_layout=}")
    print(f"get_ar_layouts - {self.kv_cache_logical_layout=}")
    print(f"get_ar_layouts - {self.ar_cache_axis_order=}")
    # get_ar_layouts - key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # get_ar_layouts - value_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # get_ar_layouts - self.kv_cache_logical_layout=('cache_batch', 'cache_sequence', 'cache_heads', 'cache_kv')
    # get_ar_layouts - self.ar_cache_axis_order=(0, 2, 1, 3)
    return key_layout, value_layout
  
  def get_ar_shapes(self, cache_logical_shape):
    # cache_logical_shape=(48, 1024, 32, 128)
    key_shape = self.cached_kv_shape(cache_logical_shape, self.ar_cache_axis_order)
    value_shape = self.cached_kv_shape(cache_logical_shape, self.ar_cache_axis_order)
    print(f"get_ar_shapes - {cache_logical_shape=}")
    print(f"get_ar_shapes - {key_shape=}")
    print(f"get_ar_shapes - {value_shape=}")
    return key_shape, value_shape
  
  def get_ar_cached_key(self, key_layout, key_shape, dtype):
    # key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # key_shape=(48, 32, 1024, 128)
    print()
    print(f"get_ar_cached_key - {key_shape=}")
    print(f"get_ar_cached_key - {key_layout=}")
    # get_ar_cached_key - key_shape=(8, 32, 1024, 128)
    # get_ar_cached_key - key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    cached_key = self.variable(
      "cache",
      "cached_ar_key",
      nn.with_logical_partitioning(jnp.zeros, key_layout),
      key_shape,
      dtype,
    )
    print(f"get_ar_cached_key - 1: {cached_key.value.shape=}")
    cached_key.value = nn.with_logical_constraint(
      cached_key.value,
      key_layout,
    )
    print(f"get_ar_cached_key - 2: {cached_key.value.shape=}")
    # get_ar_cached_key - 1: cached_key.value.shape=(8, 1024, 1024, 128)
    # get_ar_cached_key - 2: cached_key.value.shape=(8, 1024, 1024, 128)
    return cached_key

  def get_ar_cached_value(self, value_layout, value_shape, dtype):
    # value_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # value_layout=('cache_sequence', 'cache_heads', 'cache_batch', 'cache_kv')
    
    # value_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # value_layout = ['cache_batch', 'cache_sequence', 'cache_heads', 'cache_kv']
    # value_shape=(48, 32, 1024, 128)

    print(f"get_ar_cached_value - {value_layout=}")
    print(f"get_ar_cached_value - {value_shape=}")
    cached_value = self.variable(
      "cache",
      "cached_ar_value",
      nn.with_logical_partitioning(jnp.zeros, value_layout),
      value_shape,
      dtype,
    )
    cached_value.value = nn.with_logical_constraint(
      cached_value.value,
      value_layout,
    )
    return cached_value

  def _get_ar_cache(self, batch, heads, kv_head_size, quantize_kvcache):
    print()
    print(f"_get_ar_cache - {batch=}")
    print(f"_get_ar_cache - {heads=}")
    print(f"_get_ar_cache - {kv_head_size=}")

    dtype = jnp.int8 if quantize_kvcache else jnp.bfloat16
    cache_length = self.max_target_length - self.max_prefill_predict_length
    # cache_logical_shape = (batch, cache_length, heads, kv_head_size)
    cache_logical_shape = (heads, batch, cache_length, kv_head_size)
    key_layout, value_layout = self.get_ar_layouts()

    print(f"_get_ar_cache - {cache_logical_shape=}")
    print(f"_get_ar_cache - {self.kv_cache_logical_layout=}")
    print(f"_get_ar_cache - {self.ar_cache_axis_order=}")
    print(f"_get_ar_cache - {key_layout=}")
    print(f"_get_ar_cache - {value_layout=}")
    # _get_ar_cache - cache_logical_shape=(8, 32, 1024, 128)
    # _get_ar_cache - self.kv_cache_logical_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # _get_ar_cache - self.ar_cache_axis_order=(0, 1, 2, 3)
    # _get_ar_cache - key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # _get_ar_cache - value_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')


    # self.ar_cache_axis_order=(0, 2, 1, 3)
    key_shape, value_shape = self.get_ar_shapes(cache_logical_shape)

    print(f"_get_ar_cache - {key_shape=}")
    print(f"_get_ar_cache - {value_shape=}")
    # _get_ar_cache - key_shape=(8, 32, 1024, 128)
    # _get_ar_cache - value_shape=(8, 32, 1024, 128)

    # TODO(b/339703100): investigate the issue why with_logical_partitioning doesn't enforce sharding
   
    cached_key = self.get_ar_cached_key(key_layout, key_shape, dtype)
    cached_value = self.get_ar_cached_value(value_layout, value_shape, dtype)
    print(f"_get_ar_cache - {cached_key.value.shape=}")
    print(f"_get_ar_cache - {cached_value.value.shape=}")
    # _get_ar_cache - cached_key.value.shape=(8, 1024, 1024, 128)
    # _get_ar_cache - cached_value.value.shape=(8, 1024, 1024, 128)

    cache_lengths = self.variable(
        "cache",
        "cache_ar_lengths",
        nn.with_logical_partitioning(jnp.zeros, (CACHE_BATCH, )),
        (cache_logical_shape[1], ),
        jnp.int32,
    )

    cached_segment_id = self.variable(
        "cache",
        "cache_ar_segment_id",
        nn.with_logical_partitioning(jnp.zeros, (CACHE_BATCH, CACHE_SEQUENCE)),
        (cache_logical_shape[1], cache_length),
        jnp.int32,
    )

    if self.quantize_kvcache:

      cache_logical_shape_scale = (batch, cache_length, heads, 1)

      key_shape_scale = self.cached_kv_shape(cache_logical_shape_scale, self.ar_cache_axis_order)
      value_shape_scale = self.cached_kv_shape(cache_logical_shape_scale, self.ar_cache_axis_order)

      cached_key_scale_var = self.variable(
          "cache",
          "cached_ar_key_scale",
          nn.with_logical_partitioning(jnp.zeros, key_layout),
          key_shape_scale,
          jnp.bfloat16,
      )
      cached_value_scale_var = self.variable(
          "cache",
          "cached_ar_value_scale",
          nn.with_logical_partitioning(jnp.zeros, value_layout),
          value_shape_scale,
          jnp.bfloat16,
      )
    else:
      cached_key_scale_var = None
      cached_value_scale_var = None

    cache_index = self.variable("cache", "cache_ar_index", nn.with_logical_partitioning(jnp.zeros, ()), (1,), jnp.int32)
    key_vars = (cached_key, cached_key_scale_var)
    value_vars = (cached_value, cached_value_scale_var)
    return key_vars, value_vars, cached_segment_id, cache_index, cache_lengths

  def kv_cache_prefill(
      self,
      key: Array,
      value: Array,
      decoder_segment_ids: Array,
  ):
    """In prefill mode, we zero out the existing cache, run the computation and
    prepare the cache as necessary.

    Args:
      key: in shape [b, s, n, d].
      value: in shape [b, s, n, d].
      decoder_segment_ids: [b, s] -- marking segment ids for tokens

    Returns:
      key, value, decoder_segment_id.

    """
    print(f"\nkv_cache_prefill - {key.shape=}")
    print(f"kv_cache_prefill - {value.shape=}")
    # kv_cache_prefill - key.shape=(1, 32, 1024, 128)
    # kv_cache_prefill - value.shape=(1, 32, 1024, 128)
    heads, batch, sequence, kv_head_size = key.shape
    assert key.dtype == value.dtype, "Key and Value Dtypes should match."

    cached_prefill_key_var, cached_prefill_value_var, cached_prefill_segment_id = self._get_prefill_cache(
        batch, heads, kv_head_size, self.quantize_kvcache
    )
    if not self.has_variable("cache", "cache_ar_index"):
      print(f"kv_cache_prefill - {batch=}")
      print(f"kv_cache_prefill - {heads=}")
      print(f"kv_cache_prefill - {kv_head_size=}")
      cached_ar_key_var, cached_ar_value_var, _, _, _ = self._get_ar_cache(batch, heads, kv_head_size, self.quantize_kvcache)  # initialize it now
      # print(f"{self.ar_cache_axis_order=}")
      # print(f"{cached_ar_key_var[0].value.shape=}")
      # print(f"{cached_ar_value_var[0].value.shape=}")
      # print(f"{self.cached_kv_shape((batch, self.max_target_length - self.max_prefill_predict_length, heads, kv_head_size), self.ar_cache_axis_order)=}")
      # self.ar_cache_axis_order=(0, 2, 1, 3)
      # cached_ar_key_var[0].value.shape=(48, 1024, 32, 128)
      # cached_ar_value_var[0].value.shape=(48, 1024, 32, 128)
      # assert cached_ar_key_var[0].value.shape == self.cached_kv_shape((batch, self.max_target_length - self.max_prefill_predict_length, heads, kv_head_size), self.ar_cache_axis_order)
      # assert cached_ar_value_var[0].value.shape == self.cached_kv_shape((batch, self.max_target_length - self.max_prefill_predict_length, heads, kv_head_size), self.ar_cache_axis_order)
      assert cached_ar_key_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_target_length - self.max_prefill_predict_length, kv_head_size), self.ar_cache_axis_order)
      assert cached_ar_value_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_target_length - self.max_prefill_predict_length, kv_head_size), self.ar_cache_axis_order)

    print(f"kv_cache_prefill - {cached_prefill_key_var[0].value.shape=}")
    print(f"kv_cache_prefill - {cached_prefill_value_var[0].value.shape=}")
    # kv_cache_prefill - cached_prefill_key_var[0].value.shape=(1, 32, 1024, 128)
    # kv_cache_prefill - cached_prefill_value_var[0].value.shape=(1, 32, 1024, 128)
    # cached_prefill_key_var[0].value.shape=(32, 1024, 32, 128)
    # cached_prefill_value_var[0].value.shape=(32, 1024, 32, 128)
    assert cached_prefill_key_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_prefill_predict_length, kv_head_size), self.prefill_cache_axis_order)
    assert cached_prefill_value_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_prefill_predict_length, kv_head_size), self.prefill_cache_axis_order)

    prefill_key_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.prefill_cache_axis_order)
    prefill_value_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.prefill_cache_axis_order)
    print(f"kv_cache_prefill - {prefill_key_layout=}")
    print(f"kv_cache_prefill - {prefill_value_layout=}")
    # kv_cache_prefill - prefill_key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # kv_cache_prefill - prefill_value_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')

    
    key_shaped_for_cache = self.reshape_kv_cache(key, self.prefill_cache_axis_order)
    value_shaped_for_cache = self.reshape_kv_cache(value, self.prefill_cache_axis_order)
    print(f"kv_cache_prefill - {key_shaped_for_cache.shape=}")
    print(f"kv_cache_prefill - {value_shaped_for_cache.shape=}")
    # kv_cache_prefill - key_shaped_for_cache.shape=(1, 32, 1024, 128)
    # kv_cache_prefill - value_shaped_for_cache.shape=(1, 32, 1024, 128)

    if self.quantize_kvcache:
      key_shaped_for_cache, key_scale = quantizations.quantize_kv(key_shaped_for_cache, prefill_key_layout.index(CACHE_KV))
      value_shaped_for_cache, value_scale = quantizations.quantize_kv(value_shaped_for_cache, prefill_value_layout.index(CACHE_KV))
      cached_prefill_key_var[1].value = key_scale
      cached_prefill_value_var[1].value = value_scale

    cached_prefill_key_var[0].value = key_shaped_for_cache
    cached_prefill_value_var[0].value = value_shaped_for_cache

    if decoder_segment_ids is not None:
      cached_prefill_segment_id.value = decoder_segment_ids

    print(f"kv_cache_prefill - {key.shape=}")
    print(f"kv_cache_prefill - {value.shape=}")
    # kv_cache_prefill - key.shape=(1, 32, 1024, 128)
    # kv_cache_prefill - value.shape=(1, 32, 1024, 128)
    # jax.debug.print("jax.debug kv_cache_prefill key[0,0,0,:]: {}", key[0,0,0,:])
    # jax.debug.print("jax.debug kv_cache_prefill value[0,0,0,:]: {}", value[0,0,0,:])
    # jax.debug kv_cache_prefill key[0,0,0,:]: [-0.119141 -0.026123 -0.0395508 0.205078 0.789062 0.103027 -0.0830078
    # 0.617188 -0.0717773 0.0449219 -0.0415039 0.273438 -0.5 -0.527344 0.107422
    # 1.24219 0.238281 -1 0.824219 -1.49219 0.96875 1.07031 -0.886719 -0.796875
    # -0.910156 -0.8125 -0.03125 0.65625 -0.65625 -1.32812 0.945312 1.21875
    # 0.337891 0.628906 -0.644531 0.429688 -0.0449219 -0.503906 -0.992188
    # 0.289062 -0.710938 0.945312 0.310547 0.691406 0.388672 -0.734375 0.878906
    # 0.523438 0.466797 -0.667969 -0.476562 0.542969 0.5625 0.25 0.292969
    # 0.265625 -0.00680542 0.423828 0.326172 0.382812 0.253906 0.0111084
    # -0.0620117 -0.5 -0.478516 0.0537109 0.135742 -0.535156 0.200195
    # -0.0922852 -0.0062561 0.178711 -0.136719 -0.396484 1.53125 -0.0512695
    # 0.462891 -0.664062 0.201172 -0.410156 0.898438 -0.578125 0.0810547
    # -0.726562 -0.9375 -1.04688 0.789062 1.42969 1.52344 1.35938 -1.35938
    # -1.23438 1.33594 -0.90625 0.71875 0.742188 -1.35938 -1.19531 1.3125
    # -1.05469 -0.910156 0.839844 -0.5625 -0.710938 1.04688 0.5 -0.147461
    # 0.486328 0.59375 0.613281 0.0137939 -0.929688 -0.855469 0.757812
    # 0.0358887 0.0688477 0.0612793 0.324219 -0.546875 0.0605469 0.100586
    # -0.141602 0.0172119 0.0612793 0.320312 0.11084 -0.0908203 -0.0358887]
    # jax.debug kv_cache_prefill value[0,0,0,:]: [-0.00360107 0.00113678 -0.00723267 0.0157471 -0.00622559 -0.0101929
    # -0.0123291 0.000371933 -0.00296021 -0.00366211 -0.0126953 0.000999451
    # -0.00469971 0.00202942 -0.074707 -0.0050354 0.00854492 -0.0109863
    # -0.000644684 0.00234985 -0.00512695 -0.00860596 0.00213623 -1.48416e-05
    # -0.0244141 0.000197411 -0.000675201 -0.0146484 -0.00463867 0.0178223
    # 0.00343323 -0.00346375 0.0206299 -0.00460815 -0.0098877 -0.00704956
    # -0.0166016 -0.00860596 0.00643921 0.015625 0.00494385 0.00100708
    # 0.0102539 0.00270081 0.00140381 0.00817871 -0.000208855 -0.020752
    # -0.00891113 -0.00738525 -0.00543213 0.00668335 0.000839233 -0.00296021
    # 0.0206299 0.0148926 0.0145264 0.00372314 -0.00741577 -0.00531006
    # 0.0209961 -0.0201416 0.000999451 -0.00108337 -0.00994873 -0.00537109
    # -0.00927734 0.0147095 -0.00765991 0.00210571 -0.0184326 0.00256348
    # 0.0213623 0.00346375 0.00436401 -0.00448608 0.015625 -0.0167236 0.0101318
    # 0.000804901 -0.0136719 -0.0192871 0.00234985 0.00144958 -0.00343323
    # 0.0407715 0.195312 -0.00634766 0.00114441 -0.00233459 0.00158691
    # -0.0126953 -0.0250244 0.00732422 0.00145721 -0.00830078 0.00653076
    # -0.000522614 0.0106812 -0.0101929 0.00427246 0.0134888 -0.0132446
    # 0.0150146 0.00909424 -0.000341415 0.0107422 -0.00195312 -0.0164795
    # 0.0108032 0.000425339 -0.0106201 -0.00274658 0.0270996 0.000713348
    # 0.0213623 0.00350952 -0.00283813 0.0100708 -0.0098877 0.0216064
    # 0.000274658 -0.000915527 -0.00136566 -0.00527954 0.0016098 -0.0194092
    # -0.00376892]
    return key, value, decoder_segment_ids

  def update_ar_key_value(
      self,
      one_token_key: Array,
      one_token_value: Array,
      cached_key_vars: tuple[nn.Variable, nn.Variable | None],
      cached_value_vars: tuple[nn.Variable, nn.Variable | None],
      one_hot_indices: Array,
      lengths: Array,
      use_ragged: bool,
  ) -> tuple[Array, Array]:
    """Adds a single token's results to the ar kv cache

    Args:
        one_token_key (Array): Key of one token to add to the cache
        one_token_value (Array): Value of one token to add to the cache
        cached_ar_key (tuple[nn.Variable, nn.Variable|None],): Cached keys to add new token key to, possibly with scale
        cached_ar_value (tuple[nn.Variable, nn.Variable|None],: Cached values to add new token value to, possible with scale
        one_hot_indices (Array): Location of the new token within the cache
        lengths (Array): Current length of each entry in the cache

    Returns:
        tuple[Array, Array]: Updated caches for key and value with new token info added
    """

    cached_key_var, cached_key_scale_var = cached_key_vars
    cached_value_var, cached_value_scale_var = cached_value_vars

    # In order to update the key, value caches with the current key and
    # value, we reshape the one_token_key and one_token_value
    one_token_key_shaped_for_cache = self.reshape_kv_cache(one_token_key, self.ar_cache_axis_order)
    one_token_value_shaped_for_cache = self.reshape_kv_cache(one_token_value, self.ar_cache_axis_order)

    # update_ar_key_value - one_token_key.shape=(48, 1, 32, 128)
    # update_ar_key_value - one_token_key_shaped_for_cache.shape=(48, 1, 32, 128)
    print(f"update_ar_key_value - {one_token_key.shape=}")
    print(f"update_ar_key_value - {one_token_key_shaped_for_cache.shape=}")
    print(f"update_ar_key_value - {one_token_value.shape=}")
    print(f"update_ar_key_value - {one_token_value_shaped_for_cache.shape=}")
    print(f"update_ar_key_value - {lengths.shape=}")


    ar_key_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.ar_cache_axis_order)
    ar_value_layout = self.cached_kv_layout(self.kv_cache_logical_layout, self.ar_cache_axis_order)

    if self.quantize_kvcache:
      one_token_key_shaped_for_cache, one_token_key_scale = quantizations.quantize_kv(one_token_key_shaped_for_cache, ar_key_layout.index(CACHE_KV))
      one_token_value_shaped_for_cache, one_token_value_scale = quantizations.quantize_kv(one_token_value_shaped_for_cache, ar_value_layout.index(CACHE_KV))

    one_hot_indices = one_hot_indices.astype(int)

    ar_key = cached_key_var.value
    print(f"update_ar_key_value - {ar_key.shape=}")
    print(f"update_ar_key_value - {ar_key_layout=}")
    print(f"update_ar_key_value - {one_token_key_shaped_for_cache.shape=}")
    # update_ar_key_value - ar_key.shape=(48, 32, 1024, 128)
    # update_ar_key_value - ar_key_layout=('cache_batch', 'cache_heads', 'cache_sequence', 'cache_kv')
    # update_ar_key_value - one_token_key_shaped_for_cache.shape=(48, 1, 32, 128)
    if use_ragged:
      positions = [slice(None)] * len(ar_key.shape)
      positions[ar_key_layout.index(CACHE_SEQUENCE)] = lengths
      ar_key = ar_key.at[jnp.index_exp[tuple(positions)]].set(one_token_key_shaped_for_cache)
    else:
      ar_key = jax.lax.dynamic_update_index_in_dim(ar_key, one_token_key_shaped_for_cache, jnp.squeeze(one_hot_indices), ar_key_layout.index(CACHE_SEQUENCE))

    ar_key = nn.with_logical_constraint(
        ar_key,
        ar_key_layout
    )
    cached_key_var.value = ar_key

    ar_value = cached_value_var.value
    print(f"update_ar_key_value - {ar_value.shape=}")
    print(f"update_ar_key_value - {ar_value=}")
    print(f"update_ar_key_value - {one_token_value_shaped_for_cache.shape=}")
    if use_ragged:
      positions = [slice(None)] * len(ar_value.shape)
      positions[ar_value_layout.index(CACHE_SEQUENCE)] = lengths
      ar_value = ar_value.at[jnp.index_exp[tuple(positions)]].set(one_token_value_shaped_for_cache)
    else:
      ar_value = jax.lax.dynamic_update_index_in_dim(ar_value, one_token_value_shaped_for_cache, jnp.squeeze(one_hot_indices), ar_key_layout.index(CACHE_SEQUENCE))

    ar_value = nn.with_logical_constraint(
        ar_value,
        ar_value_layout,
    )
    cached_value_var.value = ar_value

    if self.quantize_kvcache:
      ar_key_scale = jax.lax.dynamic_update_index_in_dim(
          cached_key_scale_var.value, one_token_key_scale, jnp.squeeze(one_hot_indices), ar_key_layout.index(CACHE_SEQUENCE)
      )
      ar_key_scale = nn.with_logical_constraint(
          ar_key_scale,
          ar_key_layout
      )
      ar_value_scale = jax.lax.dynamic_update_index_in_dim(
          cached_value_scale_var.value, one_token_value_scale, jnp.squeeze(one_hot_indices), ar_key_layout.index(CACHE_SEQUENCE)
      )
      ar_value_scale = nn.with_logical_constraint(
          ar_value_scale,
          ar_value_layout
      )
      cached_key_scale_var.value = ar_key_scale
      cached_value_scale_var.value = ar_value_scale

      ar_key = quantizations.unquantize_kv(cached_key_var.value, cached_key_scale_var.value, one_token_key.dtype)
      ar_value = quantizations.unquantize_kv(cached_value_var.value, cached_value_scale_var.value, one_token_value.dtype)

    # Revert the keys and values back to original logical shapes.
    return self.revert_kv_cache(ar_key, self.ar_cache_axis_order), self.revert_kv_cache(ar_value, self.ar_cache_axis_order)

  def prefill_cache_var_model_var(self, cache_var, target_dtype, cache_axis_order):
    if not self.quantize_kvcache:
      return self.revert_kv_cache(cache_var[0].value, cache_axis_order)
    else:
      raw_cache, quant_scale = cache_var
      raw_cache_unquantized = quantizations.unquantize_kv(raw_cache.value, quant_scale.value, target_dtype)
      return self.revert_kv_cache(raw_cache_unquantized, cache_axis_order)

  def kv_cache_autoregressive(
      self,
      key: Array,
      value: Array,
      use_ragged: bool,
  ):
    """In autoregressive mode, we update the cache for this entry and
       then return the full cache.

    Args:
      key: in shape [b, 1, n, d].
      value: in shape [b, 1, n, d].
      decoder_segment_ids: [b, 1] -- marking segment ids for tokens

    Returns:
      tuple of (key, value, segment_id) for both prefill and ar cache,
    Raises:
      ValueError: when key/value shape is not [batch, 1, num_heads, heads_dim].
    """
    heads, batch, sequence, kv_head_size = key.shape
    print(f"kv_cache_ar - {key.shape=}")
    print(f"kv_cache_ar - {value.shape=}")
    if sequence != 1:
      raise ValueError(f"Sequence length should be 1 during autoregression, got {sequence=}")
    is_initialized = self.has_variable("cache", "cache_ar_index")
    if not is_initialized:
      raise ValueError("Error, we can't do autoregression if we haven't seeded the KV Cache.")

    print(f"kv_cache_ar - {batch=}")
    print(f"kv_cache_ar - {heads=}")
    print(f"kv_cache_ar - {kv_head_size=}")
    cached_ar_key_var, cached_ar_value_var, cached_ar_segment_id, cache_ar_index, cache_ar_lengths = self._get_ar_cache(
        batch, heads, kv_head_size, self.quantize_kvcache
    )
    # print(f"kv_cache_ar - {cache_ar_lengths.shape=}")
    # kv_cache_ar - cached_ar_key_var[0].value.shape=(8, 1024, 1024, 128)
    # kv_cache_ar - cached_ar_value_var[0].value.shape=(8, 1024, 1024, 128)
    print(f"kv_cache_ar - {cached_ar_key_var[0].value.shape=}")
    print(f"kv_cache_ar - {cached_ar_value_var[0].value.shape=}")
    assert cached_ar_key_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_target_length - self.max_prefill_predict_length, kv_head_size), self.ar_cache_axis_order)
    assert cached_ar_value_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_target_length - self.max_prefill_predict_length, kv_head_size), self.ar_cache_axis_order)

    # key = nn.with_logical_constraint(key, (BATCH, LENGTH, HEAD, D_KV))
    # value = nn.with_logical_constraint(value, (BATCH, LENGTH, HEAD, D_KV))
    key = nn.with_logical_constraint(key, (HEAD, BATCH, LENGTH, D_KV))
    value = nn.with_logical_constraint(value, (HEAD, BATCH, LENGTH, D_KV))

    ar_key, ar_value = self.update_ar_key_value(key, value, cached_ar_key_var, cached_ar_value_var, cache_ar_index.value, cache_ar_lengths.value, use_ragged)
    active_indicator = jnp.zeros((batch, 1), dtype=jnp.int32) + common_types.DECODING_ACTIVE_SEQUENCE_INDICATOR
    cached_ar_segment_id.value = jax.lax.dynamic_update_index_in_dim(
        cached_ar_segment_id.value, active_indicator, jnp.squeeze(cache_ar_index.value), 1
    )
    cache_ar_index.value = jnp.mod(cache_ar_index.value + 1, self.max_target_length - self.max_prefill_predict_length)

    cache_ar_lengths.value = cache_ar_lengths.value.at[:].add(1)
    # cache_ar_lengths.value = cache_ar_lengths.value.at[:].min(self.max_target_length - self.max_prefill_predict_length)

    # Prep and return both prefill and ar caches
    cached_prefill_key_var, cached_prefill_value_var, cached_prefill_segment_id = self._get_prefill_cache(
        batch, heads, kv_head_size, self.quantize_kvcache
    )
    assert cached_prefill_key_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_prefill_predict_length, kv_head_size), self.prefill_cache_axis_order)
    assert cached_prefill_value_var[0].value.shape == self.cached_kv_shape((heads, batch, self.max_prefill_predict_length, kv_head_size), self.prefill_cache_axis_order)

    cached_prefill = (
        self.prefill_cache_var_model_var(cached_prefill_key_var, key.dtype, self.prefill_cache_axis_order),
        self.prefill_cache_var_model_var(cached_prefill_value_var, value.dtype, self.prefill_cache_axis_order),
        cached_prefill_segment_id.value,
    )
    return cached_prefill, (ar_key, ar_value, cached_ar_segment_id.value, cache_ar_lengths.value)

  def kv_cache(self, key: Array, value: Array, decoder_segment_ids: Array, model_mode: str, use_ragged: bool) -> tuple:
    """KV cache takes the current state and updates the state accordingly.

    The key and value have dimension [b, s, n_kv, d],
    but we cache them with a reshape as defined in *_axis_order config as a TPU
    fusion optimization. This also enables the "scatter via one-hot
    broadcast" trick, which means we do a one-hot broadcast instead of a
    scatter/gather operations, resulting in a 3-4x speedup in practice.

    Args:
      key: in shape [b, s, n_kv, d].
      value: in shape [b, s, n_kv, d].
      model_mode: model mode controlling model

    Returns:
      two tuples of (k, v, decoder_segments) -- either can be Nones

    """
    if key.shape != value.shape:
      raise ValueError(f"Can't KV cache with mismatched shapes {key.shape=}, {value.shape=}")

    if model_mode == common_types.MODEL_MODE_TRAIN:
      return (key, value, decoder_segment_ids), None
    elif model_mode == common_types.MODEL_MODE_PREFILL:
      return self.kv_cache_prefill(key, value, decoder_segment_ids), None
    elif model_mode == common_types.MODEL_MODE_AUTOREGRESSIVE:
      return self.kv_cache_autoregressive(key, value, use_ragged)
    else:
      raise ValueError(f"Model Mode isn't supported! {model_mode=}")

  def normalize_attention(self, local_outs, local_maxes, local_sums):
    """Normalize across multiple localized attentions

    Args:
        local_outs (list): List of unnormalized outputs entries for each local attention
        local_maxes (list): List of max exponentials entries for each local attention
        local_sums (list): List of exponential sum entries for each local attention

    Returns:
        Array: Combined attention that has been normalized
    """
    # Based on https://github.com/google-research/google-research/blob/master/scaling_transformer_inference_efficiency/attention.py
    global_max = functools.reduce(jnp.maximum, local_maxes)
    global_sum = sum(
        [jnp.exp(local_max - global_max) * local_sum for (local_sum, local_max) in zip(local_sums, local_maxes)]
    )

    attn_out = 0
    for local_max, local_out in zip(local_maxes, local_outs):
      local_normalizer = jnp.exp(local_max - global_max) / global_sum
      attn_out += local_normalizer * local_out
    return attn_out

  @nn.compact
  def __call__(self, query, key, value, decoder_segment_ids, model_mode):
    use_ragged = True
    print(f"AttentionOp - {query.shape=}")
    print(f"AttentionOp - {key.shape=}")
    print(f"AttentionOp - {value.shape=}")
    prefill_kv_cache, ar_kv_cache = self.kv_cache(key, value, decoder_segment_ids, model_mode, use_ragged=use_ragged)

    print(f"AttentionOp - {prefill_kv_cache[0].shape=}")
    print(f"AttentionOp - {prefill_kv_cache[1].shape=}")
    if prefill_kv_cache[2] is not None:
      print(f"AttentionOp - {prefill_kv_cache[2].shape=}")
    prefill_unnormalized_output, prefill_exponentials_max, prefill_exponentials_sum = self.apply_attention(
        query=query,
        key=prefill_kv_cache[0],
        value=prefill_kv_cache[1],
        decoder_segment_ids=prefill_kv_cache[2],
        lengths=None,
        model_mode=model_mode,
        use_ragged=use_ragged,
    )
    print(f"AttentionOp - {prefill_unnormalized_output.shape=}")
    print(f"AttentionOp - {prefill_exponentials_max.shape=}")
    print(f"AttentionOp - {prefill_exponentials_sum.shape=}")
    # jax.debug.print("jax.debug AttentionOp - prefill_unnormalized_output[0,0,0,:]: {}", prefill_unnormalized_output[0,0,0,:])
    # jax.debug AttentionOp - prefill_unnormalized_output[0,0,0,:]: [-0.8125 0.570312 -0.515625 -0.53125 -0.330078 0.408203 -0.126953
    # 0.0134277 -0.527344 -1.14062 -0.785156 1.42969 0.609375 1.77344
    # -0.0766602 1.96875 -0.609375 0.714844 1.73438 -0.378906 1.29688 -0.515625
    # 1.80469 0.269531 -1.27344 -0.0644531 -1.16406 0.734375 -0.851562 -1.75781
    # 0.400391 1.6875 -0.439453 0.0062561 0.0551758 -1.22656 0.128906 0.746094
    # 0.898438 0.3125 0.9375 0.0942383 -0.000534058 -1.08594 0.957031 -0.439453
    # -1.98438 0.671875 -1.5 -0.357422 0.820312 -1.47656 -0.277344 -0.306641
    # 0.617188 1.49219 -0.135742 1.16406 -2.73438 -0.933594 -0.223633 -2.64062
    # -0.34375 0.695312 0.550781 0.361328 0.0505371 -1.22656 0.275391 0.785156
    # 0.542969 1.36719 1.28906 -0.566406 -2.15625 0.53125 0.275391 1.16406
    # 0.859375 -0.863281 -1.26562 -2.39062 -0.157227 -0.423828 -0.394531
    # -1.01562 0.429688 -0.175781 -0.208008 -0.175781 -1.90625 -0.186523
    # -0.0571289 -0.124512 -0.212891 -1.08594 0.777344 -1.74219 0.9375
    # -0.118164 1.9375 0.177734 -1.51562 0.542969 0.308594 -1.21094 -0.539062
    # -0.617188 1.13281 0.308594 -0.351562 -0.0883789 1.35938 -0.478516
    # -1.60938 -0.800781 0.929688 2.4375 -2.28125 -0.0422363 0.355469 -0.640625
    # 1.03125 0.0088501 -0.304688 0.625 0.447266 0.283203]
    # AttentionOp - prefill_unnormalized_output.shape=(1, 32, 1024, 128)
    # AttentionOp - prefill_exponentials_max.shape=(1, 32, 1024, 1)
    # AttentionOp - prefill_exponentials_sum.shape=(1, 32, 1024, 1)

    # Return the "prefill" cache if it actually the combined prefill+ar kv cache
    if ar_kv_cache is None:
      if prefill_exponentials_sum is not None:
        return prefill_unnormalized_output / prefill_exponentials_sum
      return prefill_unnormalized_output

    print(f"AttentionOp - {ar_kv_cache[0].shape=}")
    print(f"AttentionOp - {ar_kv_cache[1].shape=}")
    print(f"AttentionOp - {ar_kv_cache[2].shape=}")
    print(f"AttentionOp - {ar_kv_cache[3].shape=}")
    ar_output, ar_exponentials_max, ar_exponentials_sum = self.apply_attention(
        query=query,
        key=ar_kv_cache[0],
        value=ar_kv_cache[1],
        decoder_segment_ids=ar_kv_cache[2],
        lengths=ar_kv_cache[3],
        model_mode=model_mode,
        use_ragged=use_ragged,
    )
    print(f"AttentionOp - {ar_output.shape=}")
    print(f"AttentionOp - {ar_exponentials_max.shape=}")
    print(f"AttentionOp - {ar_exponentials_sum.shape=}")
    if ar_output is not None:
      unnormalized_outputs = [prefill_unnormalized_output, ar_output]
      exponentials_maxes = [prefill_exponentials_max, ar_exponentials_max]
      exponentials_sums = [prefill_exponentials_sum, ar_exponentials_sum]
      return self.normalize_attention(unnormalized_outputs, exponentials_maxes, exponentials_sums)
    else:
      return prefill_unnormalized_output / prefill_exponentials_sum


class Attention(nn.Module):
  """Generic Attention.

  Attributes:
    num_query_heads: number of query attention heads. Features (i.e. inputs_q.shape[-1])
      should be divisible by the number of heads.
    num_kv_heads: number of kv attention heads.
    head_dim: dimension of each head.
    mesh: Mesh, device mesh
    attention_kernel: str, guidance on if we should use an attention kernel
    dtype: the dtype of the computation.
    weight_dtype: the dtype of the weights.
    max_target_length: maximum target length
    max_prefill_predict_length: size of the maximum prefill
    dropout_rate: dropout rate
    kernel_init: initializer for the kernel of the Dense layers.
    float32_qk_product: bool, if True then compute logits via float32 qk_product to avoid
      numerical issues with bfloat16.
    float32_logits: bool, if True then cast logits to float32 before softmax to avoid
      numerical issues with bfloat16.
    quant: Quant, stores quantization parameters, defaults to None implying no quantization.
    quantize_kvcache: bool, quantize the kv cache.
  """

  config: Config
  num_query_heads: int
  num_kv_heads: int
  head_dim: int
  max_target_length: int
  mesh: Mesh
  attention_kernel: str
  dtype: DType = jnp.float32
  weight_dtype: DType = jnp.float32
  max_prefill_predict_length: int = -1
  dropout_rate: float = 0.0
  kernel_init: NdInitializer = nd_dense_init(1.0, "fan_in", "normal")
  float32_qk_product: bool = False  # computes logits in float32 for stability.
  float32_logits: bool = False  # cast logits in float32 for stability.
  quant: Optional[Quant] = None
  quantize_kvcache: bool = False

  # query_axis_names: AxisNames = (BATCH, LENGTH, HEAD, D_KV)
  # key_axis_names: AxisNames = (BATCH, LENGTH, HEAD, D_KV)
  # value_axis_names: AxisNames = (BATCH, LENGTH, HEAD, D_KV)
  # out_axis_names: AxisNames = (BATCH, LENGTH, HEAD, D_KV)
  query_axis_names: AxisNames = (HEAD, BATCH, LENGTH, D_KV)
  key_axis_names: AxisNames = (HEAD, BATCH, LENGTH, D_KV)
  value_axis_names: AxisNames = (HEAD, BATCH, LENGTH, D_KV)
  out_axis_names: AxisNames = (HEAD, BATCH, LENGTH, D_KV)

  prefill_cache_axis_order: AxisIdxes = (0, 1, 2, 3)
  ar_cache_axis_order: AxisIdxes = (0, 1, 2, 3)
  compute_axis_order: AxisIdxes = (0, 1, 2, 3)
  reshape_q: bool = False

  def query_projection(self, inputs_q: Array) -> Array:
    """Query projection."""

    # NOTE: T5 does not explicitly rescale the attention logits by
    #       1/sqrt(depth_kq)!  This is folded into the initializers of the
    #       linear transformations, which is equivalent under Adafactor.
    depth_scaling = jnp.sqrt(self.head_dim).astype(self.dtype)

    def query_init(*args):
      # pylint: disable=no-value-for-parameter
      return self.kernel_init(*args) / depth_scaling
    
    # features: tuple with numbers of output features.
    # axis: tuple with axes to apply the transformation on.
    # weight_dtype: the dtype of the weights (default: float32).
    # dtype: the dtype of the computation (default: float32).
    # kernel_init: initializer function for the weight matrix.
    # use_bias: whether to add bias in linear transformation
    # quant: quantization config, defaults to None implying no quantization.

    query_proj = DenseGeneral(
        features=(self.num_query_heads, self.head_dim),
        axis=-1,
        kernel_init=query_init,
        kernel_axes=("embed", "heads", "kv"),
        dtype=self.dtype,
        weight_dtype=self.weight_dtype,
        name="query",
        quant=self.quant,
    )(inputs_q)
    # todo(Pate) might be missing a swap here. 
    print(f"query_projection - {inputs_q.shape=}")
    print(f"query_projection - original {query_proj.shape=}")
    # query_proj = jnp.swapaxes(query_proj, 1, 2)
    query_proj = jnp.transpose(query_proj, (2, 0, 1, 3))
    print(f"query_projection - swapped {query_proj.shape=}")
    return query_proj

  def kv_projection(self, inputs_kv: Array, proj_name: str) -> Array:
    """Projection for Key and Value.

    Args:
      inputs_kv: inputs_kv: key/values of shape `[batch, kv_length,
        num_kv_heads, kv_dim]`.
      proj_name: name of projection, `key` or `value`.

    Returns:
      Projection of key or value, in shape of `[batch, kv_length, head_dim]`.
    """
    if self.num_kv_heads == -1:
      raise ValueError("num_kv_heads is not defined.")

    if self.num_query_heads % self.num_kv_heads != 0:
      raise ValueError("Invalid num_kv_heads for GQA.")
    
    print(f"kv_projection - {inputs_kv.shape=}")
    print(f"kv_projection - {proj_name=}")
    # kv_projection - inputs_kv.shape=(4, 1, 4096)
    # kv_projection - proj_name='key'

    # kv_projection - inputs_kv.shape=(4, 1, 4096)
    # kv_projection - proj_name='value'

    kv_proj = DenseGeneral(
        features=(self.num_kv_heads, self.head_dim),
        axis=-1,
        kernel_init=self.kernel_init,
        kernel_axes=("embed", "heads", "kv"),
        dtype=self.dtype,
        weight_dtype=self.weight_dtype,
        name=proj_name,
        quant=self.quant,
    )(inputs_kv)

    # todo(Pate) might be missing a swap here. 
    print(f"kv_projection - {kv_proj.shape=}")
    kv_proj = jnp.transpose(kv_proj, (2, 0, 1, 3))
    # kv_projection - kv_proj.shape=(4, 1, 32, 128)
    # kv_projection - kv_proj.shape=(4, 1, 32, 128)
    return kv_proj

  def qkv_projection(self, inputs: Array, proj_name: str):
    """Fused QKV projection"""

    qkv_proj = DenseGeneral(
        features=(3, self.num_query_heads, self.head_dim),
        axis=-1,
        kernel_init=self.kernel_init,
        kernel_axes=("embed", "qkv", "heads", "kv"),
        dtype=self.dtype,
        weight_dtype=self.weight_dtype,
        name=proj_name,
        quant=self.quant,
    )(inputs)
    qkv_proj = checkpoint_name(qkv_proj, "qkv_proj")
    query, key, value = qkv_proj[:, :, 0, ...], qkv_proj[:, :, 1, ...], qkv_proj[:, :, 2, ...]
    print(f"qkv_projection - {qkv_proj.shape=}")
    print(f"qkv_projection - {query.shape=}")
    print(f"qkv_projection - {key.shape=}")
    print(f"qkv_projection - {value.shape=}")
    return query, key, value

  def out_projection(self, output_dim: int, out: Array) -> Array:
    print(f"out_projection - {output_dim=}")
    print(f"out_projection - {out.shape=}")
    # todo(Pate) might need to change axis here
    out_proj = DenseGeneral(
        features=output_dim,
        axis=(0, -1),
        kernel_init=self.kernel_init,
        kernel_axes=("heads", "kv", "embed"),
        dtype=self.dtype,
        weight_dtype=self.weight_dtype,
        name="out",
        quant=self.quant,
    )(out)
    print(f"out_projection - {out_proj.shape=}")
    # out_projection - output_dim=4096
    # out_projection - out.shape=(32, 32, 2048, 128)
    # out_projection - out_proj.shape=(32, 32, 4096)
    return out_proj

  def key_rotary(self, key: Array, inputs_positions: Array):
    """Apply Rotary Embedding to key."""
    key = RotaryEmbedding(embedding_dims=self.head_dim, name="key_rotary")(inputs=key, position=inputs_positions)
    return key

  @nn.compact
  def __call__(
      self,
      inputs_q: Array,
      inputs_kv: Array,
      inputs_positions: Array,
      decoder_segment_ids: Array | None = None,
      *,
      model_mode: str = common_types.MODEL_MODE_TRAIN,
      deterministic: bool = False,
  ):
    """Applies Attention on the input data.

    Projects the inputs into multi-headed query, key, and value vectors,
    applies dot-product attention and project the results to an output vector.

    There are three modes: training, prefill and autoregression. During training, the KV cache
    is ignored. During prefill, the cache is filled. During autoregression the cache is used.

    In the cache initialization call, `inputs_q` has a shape [batch, length,
    q_features] and `inputs_kv`: [batch, length, kv_features]. During the
    incremental decoding stage, query, key and value all have the shape [batch,
    1, qkv_features] corresponding to a single step.

    Args:
      inputs_q: input queries of shape `[batch, q_length, q_features]`.
      inputs_kv: key/values of shape `[batch, kv_length, kv_features]`.
      model_mode: corresponding to train, prefill and decode.
      deterministic: Disables dropout if set to True.

    Returns:
      output of shape `[batch, length, q_features]`.
    """
    # apply projection.
    print(f"attention - {self.config.fused_qkv=}")
    if self.config.fused_qkv:
      query, key, value = self.qkv_projection(inputs_q, proj_name="qkv_proj")
    else:
      query = self.query_projection(inputs_q)
      key = self.kv_projection(inputs_kv, proj_name="key")
      value = self.kv_projection(inputs_kv, proj_name="value")

    print(f"attention - initial {inputs_q.shape=}")
    print(f"attention - initial {inputs_kv.shape=}")
    print(f"attention - initial {inputs_positions.shape=}")
    print(f"attention - initial {query.shape=}")
    print(f"attention - initial {key.shape=}")
    print(f"attention - initial {value.shape=}")
    # apply ROPE
    query = RotaryEmbedding(embedding_dims=self.head_dim, name="query_rotary")(inputs=query, position=inputs_positions)
    key = self.key_rotary(key, inputs_positions)
    # value = jnp.swapaxes(value, 1, 2)

    # annotate with sharding constraint.
    query = nn.with_logical_constraint(query, self.query_axis_names)
    query = checkpoint_name(query, "query_proj")
    key = nn.with_logical_constraint(key, self.key_axis_names)
    key = checkpoint_name(key, "key_proj")
    value = nn.with_logical_constraint(value, self.value_axis_names)
    value = checkpoint_name(value, "value_proj")

    attention_op = AttentionOp(
        mesh=self.mesh,
        attention_kernel=self.attention_kernel,
        max_target_length=self.max_target_length,
        max_prefill_predict_length=self.max_prefill_predict_length,
        float32_qk_product=self.float32_qk_product,
        float32_logits=self.float32_logits,
        quant=self.quant,
        quantize_kvcache=self.quantize_kvcache,
        num_query_heads=self.num_query_heads,
        num_kv_heads=self.num_kv_heads,
        dropout_rate=self.dropout_rate,
        dtype=self.dtype,
        prefill_cache_axis_order=self.prefill_cache_axis_order,
        ar_cache_axis_order=self.ar_cache_axis_order,
        compute_axis_order=self.compute_axis_order,
        reshape_q = self.reshape_q,
    )

    print(f"attention - final {query.shape=}")
    print(f"attention - final {key.shape=}")
    print(f"attention - final {value.shape=}")
    out = attention_op(query, key, value, decoder_segment_ids, model_mode)

    out = nn.with_logical_constraint(out, self.out_axis_names)

    # apply output projection,  output dim is set to the input dim.
    out = self.out_projection(inputs_q.shape[-1], out)
    out = checkpoint_name(out, "out_proj")
    print(f"Attention - {out.shape=}")
    return out
