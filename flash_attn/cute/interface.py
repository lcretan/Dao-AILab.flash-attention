# Copyright (c) 2025, Jay Shah, Ganesh Bikshandi, Ying Zhang, Vijay Thakkar, Pradeep Ramani, Tri Dao.
# [2025-06-01] Initial version in Cute-DSL.
# Only support basic forward and backward pass for FlashAttention, optimized for Ampere.
# Lightly tested with headdim 128.
# Features not supported yet:
# - varlen
# - sliding window
# - split (i.e. FlashDecoding)
# - tuned block sizes
# - paged KV
# - append KV to existing KV cache
# - FP8

import math
from typing import Optional

import torch

import cuda.bindings.driver as cuda

import cutlass
import cutlass.cute as cute

from flash_attn.cute import utils
from flash_attn.cute.flash_fwd import FlashAttentionForwardSm80
from flash_attn.cute.flash_bwd_preprocess import FlashAttentionBackwardPreprocess
from flash_attn.cute.flash_bwd import FlashAttentionBackwardSm80
from flash_attn.cute.flash_bwd_postprocess import FlashAttentionBackwardPostprocess


def maybe_contiguous(x):
    return x.contiguous() if x is not None and x.stride(-1) != 1 else x


torch2cute_dtype_map = {
    torch.float16: cutlass.Float16,
    torch.bfloat16: cutlass.BFloat16,
    torch.float32: cutlass.Float32,
}


def _flash_attn_fwd(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
    softcap: float = 0.0,
    m_block_size: int = 128,
    n_block_size: int = 64,
    num_threads: int = 128,
) -> tuple[torch.Tensor, torch.Tensor]:
    q, k, v = [maybe_contiguous(t) for t in (q, k, v)]
    batch_size, seqlen_q, num_head, head_dim = q.shape
    _, seqlen_k, num_head_kv, _ = k.shape
    _, _, _, head_dim_v = v.shape
    assert k.shape == (batch_size, seqlen_k, num_head_kv, head_dim)
    assert v.shape == (batch_size, seqlen_k, num_head_kv, head_dim_v)
    assert q.dtype in [torch.float16, torch.bfloat16], "inputs must be float16 or bfloat16"
    assert q.dtype == k.dtype == v.dtype, "inputs must have the same dtype"
    assert all(t.is_cuda for t in (q, k, v)), "inputs must be on CUDA device"
    assert num_head % num_head_kv == 0, "num_head must be divisible by num_head_kv"
    assert head_dim <= 256, "head_dim must be less than or equal to 256"
    alignment = 128 // q.element_size()
    assert head_dim % alignment == 0, f"head_dim must be divisible by {alignment}"
    assert head_dim_v % alignment == 0, f"head_dim_v must be divisible by {alignment}"
    if softmax_scale is None:
        softmax_scale = 1.0 / math.sqrt(head_dim)
    qhead_per_kvhead = num_head // num_head_kv

    out_torch_dtype = q.dtype
    device = q.device
    out = torch.empty(batch_size, seqlen_q, num_head, head_dim_v, dtype=out_torch_dtype, device=device)
    lse = torch.empty(batch_size, num_head, seqlen_q, dtype=torch.float32, device=device)

    dtype = torch2cute_dtype_map[q.dtype]
    q_tensor, k_tensor, v_tensor, o_tensor = [
        utils.convert_from_dlpack(
            t.detach(), leading_dim=3, divisibility=128 // dtype.width
        ) for t in (q, k, v, out)
    ]
    lse_tensor = utils.convert_from_dlpack(lse, leading_dim=2, alignment=4)
    current_stream = cuda.CUstream(torch.cuda.current_stream().cuda_stream)

    compile_key = (dtype, head_dim, head_dim_v, qhead_per_kvhead, causal, softcap != 0.0, m_block_size, n_block_size, num_threads)
    if compile_key not in _flash_attn_fwd.compile_cache:
        fa_fwd_sm80 = FlashAttentionForwardSm80(
            dtype,
            head_dim,
            head_dim_v,
            qhead_per_kvhead,
            m_block_size,
            n_block_size,
            num_stages=1,
            num_threads=num_threads,
            is_causal=causal,
            has_softcap=softcap != 0.0,
            Q_in_regs=False,
        )
        # TODO: check @can_implement
        _flash_attn_fwd.compile_cache[compile_key] = cute.compile(
            fa_fwd_sm80, q_tensor, k_tensor, v_tensor, o_tensor, lse_tensor,
            softmax_scale, softcap, current_stream
        )
    _flash_attn_fwd.compile_cache[compile_key](
        q_tensor, k_tensor, v_tensor, o_tensor, lse_tensor, softmax_scale, softcap, current_stream
    )
    return out, lse


_flash_attn_fwd.compile_cache = {}


def _flash_attn_bwd(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    out: torch.Tensor,
    dout: torch.Tensor,
    lse: torch.Tensor,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
    softcap: float = 0.0,
    m_block_size: int = 64,
    n_block_size: int = 128,
    num_threads: int = 256,
    num_stages_Q: int = 2,
    num_stages_dO: int = 2,
    SdP_swapAB: bool = False,
    dKV_swapAB: bool = False,
    dQ_swapAB: bool = False,
    AtomLayoutMSdP: int = 2,
    AtomLayoutNdKV: int = 2,
    AtomLayoutMdQ: int = 2,
    V_in_regs: bool = False,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    q, k, v, out, dout, lse = [maybe_contiguous(t) for t in (q, k, v, out, dout, lse)]
    batch_size, seqlen_q, num_head, head_dim = q.shape
    _, seqlen_k, num_head_kv, _ = k.shape
    _, _, _, head_dim_v = v.shape
    assert k.shape == (batch_size, seqlen_k, num_head_kv, head_dim)
    assert v.shape == (batch_size, seqlen_k, num_head_kv, head_dim_v)
    assert out.shape == (batch_size, seqlen_q, num_head, head_dim_v)
    assert dout.shape == (batch_size, seqlen_q, num_head, head_dim_v)
    assert lse.shape == (batch_size, num_head, seqlen_q), "lse must have shape (batch_size, num_head, seqlen_q)"
    assert q.dtype in [torch.float16, torch.bfloat16], "inputs must be float16 or bfloat16"
    assert q.dtype == k.dtype == v.dtype == out.dtype == dout.dtype, "inputs must have the same dtype"
    assert lse.dtype == torch.float32, "lse must be float32"
    assert all(t.is_cuda for t in (q, k, v, out, dout, lse)), "inputs must be on CUDA device"
    assert num_head % num_head_kv == 0, "num_head must be divisible by num_head_kv"
    assert head_dim <= 256, "head_dim must be less than or equal to 256"
    alignment = 128 // q.element_size()
    assert head_dim % alignment == 0, f"head_dim must be divisible by {alignment}"
    assert head_dim_v % alignment == 0, f"head_dim_v must be divisible by {alignment}"
    if softmax_scale is None:
        softmax_scale = 1.0 / math.sqrt(head_dim)
    qhead_per_kvhead = num_head // num_head_kv

    device = q.device
    # TODO: check if this is the right rounding
    seqlen_q_rounded = (seqlen_q + m_block_size - 1) // m_block_size * m_block_size
    head_dim_rounded = (head_dim + 32 - 1) // 32 * 32
    dq = torch.empty_like(q)
    dk = torch.empty_like(k)
    dv = torch.empty_like(v)
    dq_accum = torch.empty(batch_size, num_head, seqlen_q_rounded * head_dim_rounded, dtype=torch.float32, device=device)
    dpsum = torch.empty(batch_size, num_head, seqlen_q_rounded, dtype=torch.float32, device=device)
    lse_log2 = torch.empty(batch_size, num_head, seqlen_q_rounded, dtype=torch.float32, device=device)
    if qhead_per_kvhead > 1:
        seqlen_k_rounded = (seqlen_k + n_block_size - 1) // n_block_size * n_block_size
        head_dim_v_rounded = (head_dim_v + 32 - 1) // 32 * 32
        dk_accum = torch.zeros(batch_size, num_head_kv, seqlen_k_rounded * head_dim_rounded, dtype=torch.float32, device=device)
        dv_accum = torch.zeros(batch_size, num_head_kv, seqlen_k_rounded * head_dim_v_rounded, dtype=torch.float32, device=device)

    dtype = torch2cute_dtype_map[q.dtype]
    q_tensor, k_tensor, v_tensor, o_tensor, do_tensor, dq_tensor, dk_tensor, dv_tensor = [
        utils.convert_from_dlpack(
            t.detach(), leading_dim=3, divisibility=128 // dtype.width
        ) for t in (q, k, v, out, dout, dq, dk, dv)
    ]
    lse_tensor = utils.convert_from_dlpack(lse.detach(), leading_dim=2, alignment=4)
    dq_accum_tensor, dpsum_tensor, lse_log2_tensor = [
        utils.convert_from_dlpack(t.detach(), leading_dim=2, divisibility=128 // cutlass.Float32.width)
        for t in (dq_accum, dpsum, lse_log2)
    ]
    if qhead_per_kvhead > 1:
        dk_accum_tensor, dv_accum_tensor = [
            utils.convert_from_dlpack(t.detach(), leading_dim=2, divisibility=128 // cutlass.Float32.width)
            for t in (dk_accum, dv_accum)
        ]
    current_stream = cuda.CUstream(torch.cuda.current_stream().cuda_stream)

    # Preprocess kernel: compute (o * dout).sum(dim=-1), lse * log2_e, and zero out dq_accum.
    compile_key_pre = (dtype, head_dim_v, m_block_size, num_threads)
    if compile_key_pre not in _flash_attn_bwd.compile_cache_pre:
        fa_bwd_pre = FlashAttentionBackwardPreprocess(
            dtype, head_dim_v, m_block_size, num_threads=num_threads,
        )
        # TODO: check @can_implement
        _flash_attn_bwd.compile_cache_pre[compile_key_pre] = cute.compile(
            fa_bwd_pre, o_tensor, do_tensor, dpsum_tensor, lse_tensor, lse_log2_tensor,
            dq_accum_tensor, current_stream
        )
    _flash_attn_bwd.compile_cache_pre[compile_key_pre](
        o_tensor, do_tensor, dpsum_tensor, lse_tensor, lse_log2_tensor, dq_accum_tensor, current_stream
    )

    # Backward kernel: compute dk, dv, dq_accum.
    compile_key = (dtype, head_dim, head_dim_v, qhead_per_kvhead, causal, softcap != 0.0, m_block_size, n_block_size, num_threads, num_stages_Q, num_stages_dO, SdP_swapAB, dKV_swapAB, dQ_swapAB, AtomLayoutMSdP, AtomLayoutNdKV, AtomLayoutMdQ, V_in_regs)
    if compile_key not in _flash_attn_bwd.compile_cache:
        fa_bwd_sm80 = FlashAttentionBackwardSm80(
            dtype, head_dim_v, m_block_size, num_threads=num_threads,
        )
        fa_bwd_sm80 = FlashAttentionBackwardSm80(
            dtype,
            head_dim,
            head_dim_v,
            qhead_per_kvhead,
            m_block_size,
            n_block_size,
            num_stages_Q,
            num_stages_dO,
            num_threads,
            causal,
            SdP_swapAB,
            dKV_swapAB,
            dQ_swapAB,
            AtomLayoutMSdP,
            AtomLayoutNdKV,
            AtomLayoutMdQ,
            V_in_regs=V_in_regs,
        )
        # TODO: check @can_implement
        _flash_attn_bwd.compile_cache[compile_key] = cute.compile(
            fa_bwd_sm80, q_tensor, k_tensor, v_tensor, do_tensor, lse_log2_tensor, dpsum_tensor,
            dq_accum_tensor,
            dk_tensor if qhead_per_kvhead == 1 else dk_accum_tensor,
            dv_tensor if qhead_per_kvhead == 1 else dv_accum_tensor,
            softmax_scale, current_stream
        )
    _flash_attn_bwd.compile_cache[compile_key](
        q_tensor, k_tensor, v_tensor, do_tensor, lse_log2_tensor, dpsum_tensor,
        dq_accum_tensor,
        dk_tensor if qhead_per_kvhead == 1 else dk_accum_tensor,
        dv_tensor if qhead_per_kvhead == 1 else dv_accum_tensor,
        softmax_scale, current_stream
    )

    # Postprocess kernel: convert dq_accum from float32 to dq in bf16/fp16
    compile_key_post = (dtype, head_dim, m_block_size, num_threads, AtomLayoutMdQ, dQ_swapAB)
    if compile_key_post not in _flash_attn_bwd.compile_cache_post:
        fa_bwd_post = FlashAttentionBackwardPostprocess(
            dtype, head_dim, m_block_size, num_threads, AtomLayoutMdQ, dQ_swapAB
        )
        # TODO: check @can_implement
        _flash_attn_bwd.compile_cache_post[compile_key_post] = cute.compile(
            fa_bwd_post, dq_accum_tensor, dq_tensor, softmax_scale, current_stream
        )
    _flash_attn_bwd.compile_cache_post[compile_key_post](
        dq_accum_tensor, dq_tensor, softmax_scale, current_stream
    )

    if qhead_per_kvhead > 1:
        # Postprocess kernel: convert dk_accum & dv_accum from float32 to bf16/fp16
        compile_key_post = (dtype, head_dim, n_block_size, num_threads, AtomLayoutNdKV, dKV_swapAB)
        if compile_key_post not in _flash_attn_bwd.compile_cache_post:
            fa_bwd_post = FlashAttentionBackwardPostprocess(
                dtype, head_dim, n_block_size, num_threads, AtomLayoutNdKV, dKV_swapAB
            )
            # TODO: check @can_implement
            _flash_attn_bwd.compile_cache_post[compile_key_post] = cute.compile(
                fa_bwd_post, dk_accum_tensor, dk_tensor, softmax_scale, current_stream
            )
        _flash_attn_bwd.compile_cache_post[compile_key_post](
            dk_accum_tensor, dk_tensor, softmax_scale, current_stream
        )
        compile_key_post = (dtype, head_dim_v, n_block_size, num_threads, AtomLayoutNdKV, dKV_swapAB)
        if compile_key_post not in _flash_attn_bwd.compile_cache_post:
            fa_bwd_post = FlashAttentionBackwardPostprocess(
                dtype, head_dim_v, n_block_size, num_threads, AtomLayoutNdKV, dKV_swapAB
            )
            # TODO: check @can_implement
            _flash_attn_bwd.compile_cache_post[compile_key_post] = cute.compile(
                fa_bwd_post, dv_accum_tensor, dv_tensor, cutlass.Float32(1.0), current_stream
            )
        _flash_attn_bwd.compile_cache_post[compile_key_post](
            dv_accum_tensor, dv_tensor, cutlass.Float32(1.0), current_stream
        )

    return dq, dk, dv


_flash_attn_bwd.compile_cache_pre = {}
_flash_attn_bwd.compile_cache = {}
_flash_attn_bwd.compile_cache_post = {}


class FlashAttnFunc(torch.autograd.Function):

    @staticmethod
    def forward(
        ctx,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        softmax_scale: Optional[float] = None,
        causal: bool = False,
        softcap: float = 0.0,
    ):
        out, lse = _flash_attn_fwd(
            q,
            k,
            v,
            softmax_scale,
            causal=causal,
            softcap=softcap,
        )
        ctx.save_for_backward(q, k, v, out, lse)
        ctx.softmax_scale = softmax_scale
        ctx.causal = causal
        ctx.softcap = softcap
        return out, lse

    @staticmethod
    def backward(ctx, dout, *args):
        q, k, v, out, lse = ctx.saved_tensors
        dq, dk, dv = _flash_attn_bwd(
            q,
            k,
            v,
            out,
            dout,
            lse,
            ctx.softmax_scale,
            ctx.causal,
            ctx.softcap,
        )
        return dq, dk, dv, *((None,) * 3)


def flash_attn_func(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    softmax_scale: Optional[float] = None,
    causal: bool = False,
    softcap: float = 0.0,
):
    return FlashAttnFunc.apply(
        q,
        k,
        v,
        softmax_scale,
        causal,
        softcap,
    )
