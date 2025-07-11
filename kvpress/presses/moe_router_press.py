# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Union
from contextlib import contextmanager

import torch
import torch.nn as nn
import torch.nn.functional as F
import math

from kvpress.presses.base_press import BasePress
from transformers import (
    LlamaForCausalLM,
    MistralForCausalLM,
    Phi3ForCausalLM,
    Qwen2ForCausalLM,
    Qwen3ForCausalLM,
    Gemma3ForCausalLM,
    GPT2LMHeadModel,
    PreTrainedModel,
)

logger = logging.getLogger(__name__)

# 支持的模型类型
SUPPORTED_MODELS = (
    LlamaForCausalLM,
    MistralForCausalLM,
    Phi3ForCausalLM,
    Qwen2ForCausalLM,
    Qwen3ForCausalLM,
    Gemma3ForCausalLM,
    GPT2LMHeadModel,  # 添加GPT2支持
)

class BaseMoERouter(nn.Module):
    """
    基础MoE路由器类，为KV缓存压缩提供路由逻辑
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2, 
        capacity_factor: float = 1.5,
        dropout: float = 0.0
    ):
        super(BaseMoERouter, self).__init__()
        self.hidden_size = hidden_size
        self.num_experts = num_experts
        self.top_k = min(top_k, num_experts)
        self.capacity_factor = capacity_factor
        self.dropout = dropout
        
        # 路由网络
        self.router = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, num_experts)
        )
        
        # 路由统计信息
        self.register_buffer('total_tokens', torch.tensor(0.0))
        self.register_buffer('expert_usage_count', torch.zeros(num_experts))
        self.register_buffer('routing_decisions', torch.zeros(num_experts))
        
        # 初始化权重
        self._init_weights()
    
    def _init_weights(self):
        """初始化路由器权重"""
        for module in self.router:
            if isinstance(module, nn.Linear):
                nn.init.xavier_normal_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
    
    def _compute_capacity(self, batch_size: int, seq_len: int) -> int:
        """计算每个专家的容量"""
        total_tokens = batch_size * seq_len
        return int(total_tokens * self.capacity_factor * self.top_k / self.num_experts)
    
    def _create_dispatch_combine_tensors(
        self, 
        top_k_indices: torch.Tensor,
        top_k_probs: torch.Tensor,
        batch_size: int,
        seq_len: int
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """创建调度和组合张量"""
        capacity = self._compute_capacity(batch_size, seq_len)
        
        # 初始化张量
        dispatch_tensor = torch.zeros(
            batch_size, seq_len, self.num_experts, capacity,
            device=top_k_indices.device, dtype=top_k_probs.dtype
        )
        combine_tensor = torch.zeros(
            batch_size, seq_len, self.num_experts, capacity,
            device=top_k_indices.device, dtype=top_k_probs.dtype
        )
        
        # 跟踪每个专家的当前容量使用
        expert_capacity_used = torch.zeros(
            self.num_experts, device=top_k_indices.device, dtype=torch.long
        )
        
        # 填充调度和组合张量
        for b in range(batch_size):
            for s in range(seq_len):
                for k in range(self.top_k):
                    expert_idx = top_k_indices[b, s, k].item()
                    prob = top_k_probs[b, s, k].item()
                    
                    # 检查专家容量
                    if expert_capacity_used[expert_idx] < capacity:
                        pos = expert_capacity_used[expert_idx].item()
                        dispatch_tensor[b, s, expert_idx, pos] = 1.0
                        combine_tensor[b, s, expert_idx, pos] = prob
                        expert_capacity_used[expert_idx] += 1
        
        return dispatch_tensor, combine_tensor
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        路由输入到专家
        
        Args:
            hidden_states: 输入张量 [batch_size, seq_len, hidden_size]
            expert_mask: 专家可用性掩码 [num_experts]
            
        Returns:
            dispatch_tensor: 调度张量
            combine_tensor: 组合张量
            router_probs: 路由概率
            aux_loss: 辅助损失
        """
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 计算路由逻辑（分数）
        router_logits = self.router(hidden_states)  # [batch_size, seq_len, num_experts]
        
        # 应用专家掩码
        if expert_mask is not None:
            mask_value = torch.finfo(router_logits.dtype).min
            router_logits = router_logits + (1 - expert_mask) * mask_value
            
        # 计算路由概率
        router_probs = F.softmax(router_logits, dim=-1)  # [batch_size, seq_len, num_experts]
        
        # 获取top_k专家
        top_k_probs, top_k_indices = torch.topk(router_probs, k=self.top_k, dim=-1)
        
        # 重新归一化top_k概率
        top_k_probs = top_k_probs / (top_k_probs.sum(dim=-1, keepdim=True) + 1e-8)
        
        # 创建调度和组合张量
        dispatch_tensor, combine_tensor = self._create_dispatch_combine_tensors(
            top_k_indices, top_k_probs, batch_size, seq_len
        )
        
        # 计算辅助损失（负载平衡）
        aux_loss = self._compute_load_balancing_loss(router_probs, top_k_indices)
        
        # 更新统计信息
        with torch.no_grad():
            self.total_tokens += batch_size * seq_len
            # 更新专家使用计数
            for expert_idx in range(self.num_experts):
                expert_count = (top_k_indices == expert_idx).sum().float()
                self.expert_usage_count[expert_idx] += expert_count
                self.routing_decisions[expert_idx] += expert_count
        
        return dispatch_tensor, combine_tensor, router_probs, aux_loss
    
    def _compute_load_balancing_loss(
        self, 
        router_probs: torch.Tensor, 
        expert_indices: torch.Tensor
    ) -> torch.Tensor:
        """计算负载平衡损失"""
        # 计算每个专家的使用率
        router_prob_per_expert = router_probs.mean(dim=[0, 1])  # [num_experts]
        
        # 计算专家分配的实际比例
        expert_mask = F.one_hot(expert_indices, num_classes=self.num_experts).float()
        expert_usage_rate = expert_mask.mean(dim=[0, 1, 2])  # [num_experts]
        
        # 负载平衡损失：期望使用率与实际使用率的差异
        # 使用平方差损失鼓励均匀分布
        balance_loss = torch.sum(router_prob_per_expert * expert_usage_rate)
        
        return balance_loss * self.num_experts
    
    def get_routing_stats(self) -> Dict[str, torch.Tensor]:
        """获取路由统计信息"""
        if self.total_tokens > 0:
            expert_usage_ratios = self.expert_usage_count / self.total_tokens
        else:
            expert_usage_ratios = torch.zeros_like(self.expert_usage_count)
        
        return {
            "expert_usage_ratios": expert_usage_ratios,
            "expert_usage_count": self.expert_usage_count,
            "total_tokens": self.total_tokens,
            "routing_decisions": self.routing_decisions
        }
    
    def reset_stats(self):
        """重置路由统计信息"""
        self.total_tokens.zero_()
        self.expert_usage_count.zero_()
        self.routing_decisions.zero_()


class TopKBalancedRouter(BaseMoERouter):
    """
    带负载平衡的TopK路由器
    增强的负载平衡损失，支持多种平衡策略
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2,
        capacity_factor: float = 1.5,
        dropout: float = 0.0,
        balance_coefficient: float = 0.01,
        balance_mode: str = "entropy"
    ):
        super(TopKBalancedRouter, self).__init__(
            hidden_size, num_experts, top_k, capacity_factor, dropout
        )
        self.balance_coefficient = balance_coefficient
        self.balance_mode = balance_mode  # "entropy", "variance", "gini"
        
        # 平衡损失历史
        self.register_buffer('balance_loss_history', torch.zeros(100))
        self.register_buffer('balance_loss_idx', torch.tensor(0))
    
    def _compute_advanced_balance_loss(
        self, 
        router_probs: torch.Tensor
    ) -> torch.Tensor:
        """计算高级负载平衡损失"""
        # 计算每个专家的平均概率
        expert_probs = router_probs.mean(dim=[0, 1])  # [num_experts]
        
        if self.balance_mode == "entropy":
            # 最大化熵以实现均匀分布
            entropy = -torch.sum(expert_probs * torch.log(expert_probs + 1e-8))
            max_entropy = math.log(self.num_experts)
            balance_loss = 1.0 - (entropy / max_entropy)
            
        elif self.balance_mode == "variance":
            # 最小化方差
            target_prob = 1.0 / self.num_experts
            balance_loss = torch.var(expert_probs - target_prob)
            
        elif self.balance_mode == "gini":
            # 最小化基尼系数
            sorted_probs, _ = torch.sort(expert_probs)
            index = torch.arange(1, self.num_experts + 1, dtype=torch.float, device=expert_probs.device)
            gini = (2 * torch.sum(index * sorted_probs) / (self.num_experts * torch.sum(sorted_probs))) - 1
            balance_loss = gini
            
        else:
            # 默认使用基础损失
            balance_loss = super()._compute_load_balancing_loss(router_probs, None)
        
        return balance_loss
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        dispatch_tensor, combine_tensor, router_probs, base_aux_loss = super().forward(
            hidden_states, expert_mask
        )
        
        # 计算高级平衡损失
        advanced_balance_loss = self._compute_advanced_balance_loss(router_probs)
        
        # 组合损失
        total_aux_loss = base_aux_loss + self.balance_coefficient * advanced_balance_loss
        
        # 记录平衡损失历史
        with torch.no_grad():
            idx = self.balance_loss_idx.item() % 100
            self.balance_loss_history[idx] = advanced_balance_loss.item()
            self.balance_loss_idx += 1
        
        return dispatch_tensor, combine_tensor, router_probs, total_aux_loss
    
    def get_balance_loss_stats(self) -> Dict[str, float]:
        """获取平衡损失统计信息"""
        valid_entries = min(self.balance_loss_idx.item(), 100)
        if valid_entries > 0:
            recent_losses = self.balance_loss_history[:valid_entries]
            return {
                "avg_balance_loss": recent_losses.mean().item(),
                "min_balance_loss": recent_losses.min().item(),
                "max_balance_loss": recent_losses.max().item(),
                "balance_loss_std": recent_losses.std().item()
            }
        return {}


class AdaptiveRouter(BaseMoERouter):
    """
    自适应路由器
    基于输入重要性调整路由策略
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2,
        capacity_factor: float = 1.5,
        dropout: float = 0.0,
        importance_threshold: float = 0.5,
        adaptive_top_k: bool = True
    ):
        super(AdaptiveRouter, self).__init__(
            hidden_size, num_experts, top_k, capacity_factor, dropout
        )
        self.importance_threshold = importance_threshold
        self.adaptive_top_k = adaptive_top_k
        
        # 重要性预测网络
        self.importance_predictor = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, 1),
            nn.Sigmoid()
        )
        
        # 动态top_k预测器（如果启用）
        if adaptive_top_k:
            self.top_k_predictor = nn.Sequential(
                nn.Linear(hidden_size + 1, hidden_size // 4),  # +1 for importance
                nn.ReLU(),
                nn.Linear(hidden_size // 4, top_k),
                nn.Softmax(dim=-1)
            )
        
        # 重要性统计
        self.register_buffer('importance_history', torch.zeros(1000))
        self.register_buffer('importance_idx', torch.tensor(0))
    
    def _compute_importance(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """计算输入重要性分数"""
        importance = self.importance_predictor(hidden_states)  # [batch_size, seq_len, 1]
        return importance.squeeze(-1)  # [batch_size, seq_len]
    
    def _adapt_top_k(self, hidden_states: torch.Tensor, importance: torch.Tensor) -> int:
        """自适应调整top_k值"""
        if not self.adaptive_top_k or not hasattr(self, 'top_k_predictor'):
            return self.top_k
        
        # 计算平均特征和重要性
        avg_features = hidden_states.mean(dim=[0, 1])  # [hidden_size]
        avg_importance = importance.mean().unsqueeze(0)  # [1]
        
        # 组合特征
        combined_features = torch.cat([avg_features, avg_importance])
        
        # 预测top_k分布
        top_k_probs = self.top_k_predictor(combined_features)  # [top_k]
        
        # 选择概率最高的k值（1到top_k之间）
        adaptive_k = torch.argmax(top_k_probs).item() + 1
        
        return min(adaptive_k, self.top_k)
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 计算重要性分数
        importance = self._compute_importance(hidden_states)  # [batch_size, seq_len]
        
        # 自适应调整top_k
        current_top_k = self._adapt_top_k(hidden_states, importance)
        
        # 临时调整top_k进行路由计算
        original_top_k = self.top_k
        self.top_k = current_top_k
        
        # 执行基础路由
        dispatch_tensor, combine_tensor, router_probs, aux_loss = super().forward(
            hidden_states, expert_mask
        )
        
        # 恢复原始top_k
        self.top_k = original_top_k
        
        # 基于重要性调整路由概率
        importance_weight = (importance > self.importance_threshold).float()
        
        # 调整组合张量：重要token获得更高权重
        importance_expanded = importance_weight.unsqueeze(-1).unsqueeze(-1)  # [batch_size, seq_len, 1, 1]
        combine_tensor = combine_tensor * (1.0 + importance_expanded)
        
        # 记录重要性历史
        with torch.no_grad():
            flat_importance = importance.flatten()
            for imp_val in flat_importance:
                idx = self.importance_idx.item() % 1000
                self.importance_history[idx] = imp_val.item()
                self.importance_idx += 1
        
        return dispatch_tensor, combine_tensor, router_probs, aux_loss, importance


class PiKVMoERouter(AdaptiveRouter):
    """
    PiKV专用MoE路由器
    结合KV缓存使用情况进行路由决策
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2,
        capacity_factor: float = 1.5,
        dropout: float = 0.0,
        importance_threshold: float = 0.5,
        cache_aware: bool = True,
        cache_update_interval: int = 100
    ):
        super(PiKVMoERouter, self).__init__(
            hidden_size, num_experts, top_k, capacity_factor, 
            dropout, importance_threshold, True
        )
        self.cache_aware = cache_aware
        self.cache_update_interval = cache_update_interval
        
        # 缓存使用情况跟踪
        self.register_buffer('cache_usage_history', torch.zeros(num_experts, 100))
        self.register_buffer('cache_hit_rates', torch.zeros(num_experts))
        self.register_buffer('cache_update_counter', torch.tensor(0))
        
        # 缓存感知路由调整网络
        if cache_aware:
            self.cache_router_adjustment = nn.Sequential(
                nn.Linear(hidden_size + num_experts, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, num_experts),
                nn.Tanh()  # 输出调整因子 [-1, 1]
            )
    
    def update_cache_usage(self, expert_idx: int, cache_hit_rate: float):
        """更新专家的缓存使用情况"""
        if 0 <= expert_idx < self.num_experts:
            # 更新命中率
            self.cache_hit_rates[expert_idx] = cache_hit_rate
            
            # 更新历史记录
            history_idx = self.cache_update_counter.item() % 100
            self.cache_usage_history[expert_idx, history_idx] = cache_hit_rate
            
        self.cache_update_counter += 1
    
    def _compute_cache_aware_adjustment(
        self, 
        hidden_states: torch.Tensor,
        router_logits: torch.Tensor
    ) -> torch.Tensor:
        """计算缓存感知的路由调整"""
        if not self.cache_aware or not hasattr(self, 'cache_router_adjustment'):
            return torch.zeros_like(router_logits)
        
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 计算输入特征的平均值
        avg_features = hidden_states.mean(dim=1)  # [batch_size, hidden_size]
        
        # 扩展缓存命中率到批次维度
        cache_rates_expanded = self.cache_hit_rates.unsqueeze(0).expand(batch_size, -1)
        
        # 组合特征
        combined_input = torch.cat([avg_features, cache_rates_expanded], dim=-1)
        
        # 计算调整因子
        adjustment_factors = self.cache_router_adjustment(combined_input)  # [batch_size, num_experts]
        
        # 扩展到序列维度
        adjustment_factors = adjustment_factors.unsqueeze(1).expand(-1, seq_len, -1)
        
        return adjustment_factors
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        kv_cache_states: Optional[torch.Tensor] = None,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 计算基础路由逻辑
        router_logits = self.router(hidden_states)
        
        # 应用缓存感知调整
        if self.cache_aware:
            cache_adjustments = self._compute_cache_aware_adjustment(hidden_states, router_logits)
            router_logits = router_logits + cache_adjustments
        
        # 应用专家掩码
        if expert_mask is not None:
            mask_value = torch.finfo(router_logits.dtype).min
            router_logits = router_logits + (1 - expert_mask) * mask_value
        
        # 计算路由概率
        router_probs = F.softmax(router_logits, dim=-1)
        
        # 计算重要性
        importance = self._compute_importance(hidden_states)
        
        # 自适应top_k
        current_top_k = self._adapt_top_k(hidden_states, importance)
        
        # 获取top_k专家
        top_k_probs, top_k_indices = torch.topk(router_probs, k=current_top_k, dim=-1)
        
        # 重新归一化
        top_k_probs = top_k_probs / (top_k_probs.sum(dim=-1, keepdim=True) + 1e-8)
        
        # 创建调度和组合张量
        original_top_k = self.top_k
        self.top_k = current_top_k
        dispatch_tensor, combine_tensor = self._create_dispatch_combine_tensors(
            top_k_indices, top_k_probs, batch_size, seq_len
        )
        self.top_k = original_top_k
        
        # 计算辅助损失
        aux_loss = self._compute_load_balancing_loss(router_probs, top_k_indices)
        
        # 更新统计信息
        with torch.no_grad():
            self.total_tokens += batch_size * seq_len
            for expert_idx in range(self.num_experts):
                expert_count = (top_k_indices == expert_idx).sum().float()
                self.expert_usage_count[expert_idx] += expert_count
        
        return dispatch_tensor, combine_tensor, router_probs, aux_loss, importance


class EPLBRouter(BaseMoERouter):
    """
    精确负载平衡路由器 (Exact Perfect Load Balancing)
    实现严格的负载平衡约束
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2,
        capacity_factor: float = 1.0,  # 更严格的容量限制
        dropout: float = 0.0,
        balance_coefficient: float = 0.1,
        temperature: float = 1.0
    ):
        super(EPLBRouter, self).__init__(
            hidden_size, num_experts, top_k, capacity_factor, dropout
        )
        self.balance_coefficient = balance_coefficient
        self.temperature = temperature
        
        # 专家权重动态调整
        self.register_buffer('expert_weights', torch.ones(num_experts))
        self.register_buffer('expert_load_history', torch.zeros(num_experts, 50))
        self.register_buffer('load_history_idx', torch.tensor(0))
    
    def _update_expert_weights(self, expert_usage: torch.Tensor):
        """动态更新专家权重以平衡负载"""
        # 记录当前使用情况
        idx = self.load_history_idx.item() % 50
        self.expert_load_history[:, idx] = expert_usage
        self.load_history_idx += 1
        
        # 计算平均负载
        valid_entries = min(self.load_history_idx.item(), 50)
        if valid_entries > 0:
            avg_loads = self.expert_load_history[:, :valid_entries].mean(dim=1)
            
            # 调整权重：负载高的专家降低权重，负载低的专家提高权重
            target_load = avg_loads.mean()
            weight_adjustments = 1.0 - (avg_loads - target_load) / (target_load + 1e-8)
            
            # 平滑更新权重
            self.expert_weights = 0.9 * self.expert_weights + 0.1 * weight_adjustments
            self.expert_weights = torch.clamp(self.expert_weights, 0.1, 2.0)
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 计算路由逻辑
        router_logits = self.router(hidden_states)
        
        # 应用专家权重调整
        expert_weights_expanded = self.expert_weights.unsqueeze(0).unsqueeze(0)
        router_logits = router_logits * expert_weights_expanded
        
        # 应用温度缩放
        router_logits = router_logits / self.temperature
        
        # 应用专家掩码
        if expert_mask is not None:
            mask_value = torch.finfo(router_logits.dtype).min
            router_logits = router_logits + (1 - expert_mask) * mask_value
        
        # 计算路由概率
        router_probs = F.softmax(router_logits, dim=-1)
        
        # 获取top_k专家
        top_k_probs, top_k_indices = torch.topk(router_probs, k=self.top_k, dim=-1)
        
        # 重新归一化
        top_k_probs = top_k_probs / (top_k_probs.sum(dim=-1, keepdim=True) + 1e-8)
        
        # 创建调度和组合张量
        dispatch_tensor, combine_tensor = self._create_dispatch_combine_tensors(
            top_k_indices, top_k_probs, batch_size, seq_len
        )
        
        # 计算精确负载平衡损失
        aux_loss = self._compute_exact_balance_loss(router_probs, top_k_indices)
        
        # 更新专家权重
        with torch.no_grad():
            expert_usage = torch.zeros(self.num_experts, device=hidden_states.device)
            for expert_idx in range(self.num_experts):
                expert_usage[expert_idx] = (top_k_indices == expert_idx).sum().float()
            self._update_expert_weights(expert_usage)
        
        return dispatch_tensor, combine_tensor, router_probs, aux_loss
    
    def _compute_exact_balance_loss(
        self, 
        router_probs: torch.Tensor, 
        expert_indices: torch.Tensor
    ) -> torch.Tensor:
        """计算精确负载平衡损失"""
        # 计算实际专家使用分布
        expert_counts = torch.zeros(self.num_experts, device=router_probs.device)
        for expert_idx in range(self.num_experts):
            expert_counts[expert_idx] = (expert_indices == expert_idx).sum().float()
        
        # 目标是均匀分布
        total_assignments = expert_counts.sum()
        target_count = total_assignments / self.num_experts
        
        # 计算与理想分布的偏差
        balance_loss = torch.sum((expert_counts - target_count) ** 2) / total_assignments
        
        return self.balance_coefficient * balance_loss


class HierarchicalRouter(BaseMoERouter):
    """
    层次化路由器
    首先路由到专家组，然后在组内进行二级路由
    """
    def __init__(
        self, 
        hidden_size: int, 
        num_experts: int, 
        top_k: int = 2,
        capacity_factor: float = 1.5,
        dropout: float = 0.0,
        num_groups: int = 4,
        group_top_k: int = 1
    ):
        super(HierarchicalRouter, self).__init__(
            hidden_size, num_experts, top_k, capacity_factor, dropout
        )
        self.num_groups = num_groups
        self.group_top_k = group_top_k
        self.experts_per_group = num_experts // num_groups
        
        # 组级路由器
        self.group_router = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_groups)
        )
        
        # 组内路由器
        self.intra_group_routers = nn.ModuleList([
            nn.Sequential(
                nn.Linear(hidden_size, hidden_size // 4),
                nn.ReLU(),
                nn.Linear(hidden_size // 4, self.experts_per_group)
            ) for _ in range(num_groups)
        ])
        
        # 初始化权重
        self._init_hierarchical_weights()
    
    def _init_hierarchical_weights(self):
        """初始化层次化路由器权重"""
        # 初始化组路由器
        for module in self.group_router:
            if isinstance(module, nn.Linear):
                nn.init.xavier_normal_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
        
        # 初始化组内路由器
        for group_router in self.intra_group_routers:
            for module in group_router:
                if isinstance(module, nn.Linear):
                    nn.init.xavier_normal_(module.weight)
                    if module.bias is not None:
                        nn.init.zeros_(module.bias)
    
    def forward(
        self, 
        hidden_states: torch.Tensor,
        expert_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        batch_size, seq_len, hidden_size = hidden_states.shape
        
        # 第一阶段：路由到专家组
        group_logits = self.group_router(hidden_states)  # [batch_size, seq_len, num_groups]
        group_probs = F.softmax(group_logits, dim=-1)
        
        # 选择top_k组
        top_group_probs, top_group_indices = torch.topk(
            group_probs, k=min(self.group_top_k, self.num_groups), dim=-1
        )
        
        # 初始化最终路由结果
        final_expert_probs = torch.zeros(
            batch_size, seq_len, self.num_experts, device=hidden_states.device
        )
        
        # 第二阶段：在每个选中的组内进行路由
        for group_idx in range(min(self.group_top_k, self.num_groups)):
            # 获取当前组的索引和概率
            current_group_indices = top_group_indices[:, :, group_idx]  # [batch_size, seq_len]
            current_group_probs = top_group_probs[:, :, group_idx]  # [batch_size, seq_len]
            
            # 对每个组进行组内路由
            for g in range(self.num_groups):
                # 找到属于当前组的位置
                group_mask = (current_group_indices == g)  # [batch_size, seq_len]
                if not group_mask.any():
                    continue
                
                # 提取属于当前组的hidden states
                group_hidden = hidden_states[group_mask]  # [num_tokens, hidden_size]
                
                if group_hidden.size(0) == 0:
                    continue
                
                # 组内路由
                intra_group_logits = self.intra_group_routers[g](group_hidden)
                intra_group_probs = F.softmax(intra_group_logits, dim=-1)
                
                # 计算最终专家概率（组概率 × 组内概率）
                group_prob_values = current_group_probs[group_mask].unsqueeze(-1)  # [num_tokens, 1]
                final_intra_probs = intra_group_probs * group_prob_values  # [num_tokens, experts_per_group]
                
                # 映射到全局专家索引
                expert_start_idx = g * self.experts_per_group
                expert_end_idx = expert_start_idx + self.experts_per_group
                
                # 将组内概率映射到全局专家概率张量
                # 使用scatter_来正确映射
                for i in range(self.experts_per_group):
                    expert_idx = expert_start_idx + i
                    if expert_idx < self.num_experts:
                        # 创建索引张量
                        batch_indices, seq_indices = torch.where(group_mask)
                        if len(batch_indices) > 0:
                            final_expert_probs[batch_indices, seq_indices, expert_idx] = final_intra_probs[:, i]
        
        # 获取top_k专家
        top_k_probs, top_k_indices = torch.topk(final_expert_probs, k=self.top_k, dim=-1)
        
        # 重新归一化
        top_k_probs = top_k_probs / (top_k_probs.sum(dim=-1, keepdim=True) + 1e-8)
        
        # 创建调度和组合张量
        dispatch_tensor, combine_tensor = self._create_dispatch_combine_tensors(
            top_k_indices, top_k_probs, batch_size, seq_len
        )
        
        # 计算层次化损失（组级 + 专家级）
        # 组级损失：使用组概率和组索引
        group_balance_loss = self._compute_group_balance_loss(group_probs, top_group_indices)
        # 专家级损失：使用专家概率和专家索引
        expert_balance_loss = self._compute_load_balancing_loss(final_expert_probs, top_k_indices)
        aux_loss = group_balance_loss + expert_balance_loss
        
        return dispatch_tensor, combine_tensor, final_expert_probs, aux_loss
    
    def _compute_group_balance_loss(
        self, 
        group_probs: torch.Tensor, 
        group_indices: torch.Tensor
    ) -> torch.Tensor:
        """计算组级负载平衡损失"""
        # 计算每个组的使用率
        group_prob_per_group = group_probs.mean(dim=[0, 1])  # [num_groups]
        
        # 计算组分配的实际比例
        group_mask = F.one_hot(group_indices, num_classes=self.num_groups).float()
        group_usage_rate = group_mask.mean(dim=[0, 1, 2])  # [num_groups]
        
        # 负载平衡损失：期望使用率与实际使用率的差异
        balance_loss = torch.sum(group_prob_per_group * group_usage_rate)
        
        return balance_loss * self.num_groups


@dataclass
class MoERouterPress(BasePress):
    """
    MoE路由器Press，将MoE路由逻辑集成到KV缓存压缩中
    
    该Press使用MoE路由器来决定如何压缩KV缓存：
    - 根据输入特征路由到不同的专家
    - 每个专家负责不同的压缩策略
    - 支持缓存感知的路由决策
    """
    
    num_experts: int = 4
    top_k: int = 2
    capacity_factor: float = 1.5
    dropout: float = 0.1
    router_type: str = "pikv"  # "base", "topk_balanced", "adaptive", "pikv", "eplb", "hierarchical"
    cache_aware: bool = True
    compression_ratio: float = 0.5  # 目标压缩比
    aux_loss_weight: float = 0.01
    
    # 路由器特定参数
    balance_coefficient: float = 0.01
    balance_mode: str = "entropy"  # "entropy", "variance", "gini"
    importance_threshold: float = 0.5
    adaptive_top_k: bool = True
    temperature: float = 1.0
    num_groups: int = 4
    group_top_k: int = 1
    
    def __post_init__(self):
        self.routers = {}
        self.expert_compression_stats = {}
        self.total_aux_loss = 0.0
        self.forward_count = 0
        
        # 初始化专家压缩策略
        self.expert_strategies = {
            0: "aggressive",    # 激进压缩
            1: "moderate",      # 中等压缩
            2: "conservative",  # 保守压缩
            3: "selective"      # 选择性压缩
        }
    
    def _get_router(self, layer_idx: int, hidden_size: int) -> BaseMoERouter:
        """获取或创建路由器"""
        if layer_idx not in self.routers:
            if self.router_type == "topk_balanced":
                router = TopKBalancedRouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout,
                    balance_coefficient=self.balance_coefficient,
                    balance_mode=self.balance_mode
                )
            elif self.router_type == "adaptive":
                router = AdaptiveRouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout,
                    importance_threshold=self.importance_threshold,
                    adaptive_top_k=self.adaptive_top_k
                )
            elif self.router_type == "pikv":
                router = PiKVMoERouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout,
                    importance_threshold=self.importance_threshold,
                    cache_aware=self.cache_aware
                )
            elif self.router_type == "eplb":
                router = EPLBRouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout,
                    balance_coefficient=self.balance_coefficient,
                    temperature=self.temperature
                )
            elif self.router_type == "hierarchical":
                router = HierarchicalRouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout,
                    num_groups=self.num_groups,
                    group_top_k=self.group_top_k
                )
            else:
                # 默认使用基础路由器
                router = BaseMoERouter(
                    hidden_size=hidden_size,
                    num_experts=self.num_experts,
                    top_k=self.top_k,
                    capacity_factor=self.capacity_factor,
                    dropout=self.dropout
                )
            
            self.routers[layer_idx] = router
            self.expert_compression_stats[layer_idx] = {
                "expert_usage": torch.zeros(self.num_experts),
                "compression_ratios": torch.zeros(self.num_experts),
                "cache_hit_rates": torch.zeros(self.num_experts)
            }
        
        return self.routers[layer_idx]
    
    def _apply_expert_compression(
        self, 
        keys: torch.Tensor, 
        values: torch.Tensor, 
        strategy: str,
        router_probs: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """应用专家特定的压缩策略"""
        batch_size, num_heads, seq_len, head_dim = keys.shape
        
        if strategy == "aggressive":
            # 激进压缩：保留前20%和后10%
            keep_front = max(1, int(seq_len * 0.2))
            keep_back = max(1, int(seq_len * 0.1))
            if seq_len > keep_front + keep_back:
                keys = torch.cat([keys[:, :, :keep_front], keys[:, :, -keep_back:]], dim=2)
                values = torch.cat([values[:, :, :keep_front], values[:, :, -keep_back:]], dim=2)
                
        elif strategy == "moderate":
            # 中等压缩：保留前30%和后20%
            keep_front = max(1, int(seq_len * 0.3))
            keep_back = max(1, int(seq_len * 0.2))
            if seq_len > keep_front + keep_back:
                keys = torch.cat([keys[:, :, :keep_front], keys[:, :, -keep_back:]], dim=2)
                values = torch.cat([values[:, :, :keep_front], values[:, :, -keep_back:]], dim=2)
                
        elif strategy == "conservative":
            # 保守压缩：保留前50%和后30%
            keep_front = max(1, int(seq_len * 0.5))
            keep_back = max(1, int(seq_len * 0.3))
            if seq_len > keep_front + keep_back:
                keys = torch.cat([keys[:, :, :keep_front], keys[:, :, -keep_back:]], dim=2)
                values = torch.cat([values[:, :, :keep_front], values[:, :, -keep_back:]], dim=2)
                
        elif strategy == "selective":
            # 选择性压缩：基于注意力权重选择重要位置
            # 使用路由概率作为重要性指标
            importance_scores = router_probs.mean(dim=0)  # [seq_len, num_experts]
            importance_scores = importance_scores.mean(dim=-1)  # [seq_len]
            
            # 选择重要性最高的位置
            num_keep = max(1, int(seq_len * (1 - self.compression_ratio)))
            # 确保k值不超过序列长度
            num_keep = min(num_keep, seq_len)
            
            # 确保importance_scores有正确的维度
            if importance_scores.dim() == 0:
                # 如果是标量，扩展为序列长度
                importance_scores = importance_scores.expand(seq_len)
            elif importance_scores.dim() > 1:
                # 如果是多维，取平均值
                importance_scores = importance_scores.mean(dim=-1)
            
            # 确保序列长度匹配
            if importance_scores.size(0) != seq_len:
                # 如果不匹配，使用简单的线性插值
                importance_scores = torch.linspace(0, 1, seq_len, device=importance_scores.device)
            
            _, important_indices = torch.topk(importance_scores, k=num_keep, dim=-1)
            important_indices = torch.sort(important_indices)[0]  # 保持顺序
            
            keys = keys[:, :, important_indices, :]
            values = values[:, :, important_indices, :]
        
        return keys, values
    
    def _compute_cache_hit_rate(self, keys: torch.Tensor, values: torch.Tensor) -> float:
        """计算缓存命中率（简化版本）"""
        # 这里可以实现更复杂的缓存命中率计算
        # 目前使用序列长度作为简单指标
        return min(1.0, keys.shape[2] / 1000.0)  # 假设1000是理想长度
    
    def compress(
        self,
        module: nn.Module,
        hidden_states: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attentions: torch.Tensor,
        kwargs: dict,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        使用MoE路由器进行KV缓存压缩
        """
        layer_idx = module.layer_idx
        hidden_size = hidden_states.shape[-1]
        
        # 获取路由器
        router = self._get_router(layer_idx, hidden_size)
        
        # 执行路由
        if isinstance(router, (AdaptiveRouter, PiKVMoERouter)):
            # 这些路由器返回5个值，包括重要性分数
            dispatch_tensor, combine_tensor, router_probs, aux_loss, importance = router(
                hidden_states, expert_mask=None
            )
        else:
            # 其他路由器返回4个值
            dispatch_tensor, combine_tensor, router_probs, aux_loss = router(
                hidden_states, expert_mask=None
            )
            importance = None
        
        # 累积辅助损失
        self.total_aux_loss += aux_loss.item()
        self.forward_count += 1
        
        # 获取每个专家的路由概率
        expert_probs = router_probs.mean(dim=[0, 1])  # [num_experts]
        
        # 选择概率最高的专家
        dominant_expert = torch.argmax(expert_probs).item()
        strategy = self.expert_strategies[dominant_expert]
        
        # 应用专家压缩策略
        compressed_keys, compressed_values = self._apply_expert_compression(
            keys, values, strategy, router_probs
        )
        
        # 更新统计信息
        with torch.no_grad():
            # 更新专家使用统计
            self.expert_compression_stats[layer_idx]["expert_usage"][dominant_expert] += 1
            
            # 计算压缩比
            original_size = keys.shape[2]
            compressed_size = compressed_keys.shape[2]
            compression_ratio = (original_size - compressed_size) / original_size
            self.expert_compression_stats[layer_idx]["compression_ratios"][dominant_expert] += compression_ratio
            
            # 更新缓存命中率
            cache_hit_rate = self._compute_cache_hit_rate(compressed_keys, compressed_values)
            self.expert_compression_stats[layer_idx]["cache_hit_rates"][dominant_expert] += cache_hit_rate
            
            # 如果是PiKV路由器，更新缓存使用情况
            if isinstance(router, PiKVMoERouter):
                router.update_cache_usage(dominant_expert, cache_hit_rate)
        
        logger.debug(f"Layer {layer_idx}: Expert {dominant_expert} ({strategy}) "
                    f"compressed {keys.shape[2]} -> {compressed_keys.shape[2]} "
                    f"(ratio: {compression_ratio:.3f})")
        
        return compressed_keys, compressed_values
    
    def get_stats(self) -> Dict[str, Union[float, Dict]]:
        """获取MoE路由器统计信息"""
        stats = {
            "total_aux_loss": self.total_aux_loss,
            "avg_aux_loss": self.total_aux_loss / max(1, self.forward_count),
            "forward_count": self.forward_count,
            "layer_stats": {}
        }
        
        for layer_idx, router in self.routers.items():
            layer_stats = {
                "router_stats": router.get_routing_stats(),
                "expert_compression_stats": self.expert_compression_stats[layer_idx]
            }
            stats["layer_stats"][layer_idx] = layer_stats
        
        return stats
    
    def reset_stats(self):
        """重置统计信息"""
        self.total_aux_loss = 0.0
        self.forward_count = 0
        
        for router in self.routers.values():
            router.reset_stats()
        
        for layer_stats in self.expert_compression_stats.values():
            layer_stats["expert_usage"].zero_()
            layer_stats["compression_ratios"].zero_()
            layer_stats["cache_hit_rates"].zero_()
    
    def forward_hook(self, module: nn.Module, input: list[torch.Tensor], kwargs: dict, output: list):
        """
        重写forward_hook方法以支持GPT2和其他模型
        """
        # 检测模型类型并提取参数
        if isinstance(module, torch.nn.Module) and hasattr(module, '_model_type'):
            # GPT2模型：参数在input中
            if module._model_type == 'gpt2':
                hidden_states = input[0] if input else None
                # GPT2可能没有past_key_value，需要特殊处理
                if len(input) > 1 and input[1] is not None:
                    cache = input[1]
                else:
                    # 如果没有缓存，直接返回
                    return output
            else:
                # 其他模型：参数在kwargs中
                hidden_states = kwargs.get("hidden_states")
                cache = kwargs.get("past_key_value")
        else:
            # 默认尝试从kwargs获取
            hidden_states = kwargs.get("hidden_states")
            cache = kwargs.get("past_key_value")
        
        # 如果没有hidden_states，尝试从input获取
        if hidden_states is None and input:
            hidden_states = input[0]
        
        # 如果仍然没有hidden_states，直接返回
        if hidden_states is None:
            return output
        
        # 如果没有缓存，直接返回
        if cache is None:
            return output
        
        q_len = hidden_states.shape[1]
        
        # 检查是否需要压缩（简化版本，不检查cache_position）
        # 对于GPT2，我们总是尝试压缩
        
        # 获取缓存
        if hasattr(cache, 'key_cache') and hasattr(cache, 'value_cache'):
            # 标准缓存格式
            keys = cache.key_cache[module.layer_idx]
            values = cache.value_cache[module.layer_idx]
        else:
            # 可能是其他格式，直接返回
            return output
        
        # 执行压缩
        keys, values = self.compress(module, hidden_states, keys, values, output[1] if len(output) > 1 else None, kwargs)
        
        # 更新缓存
        cache.key_cache[module.layer_idx] = keys
        cache.value_cache[module.layer_idx] = values
        
        return output
    
    @contextmanager
    def __call__(self, model: PreTrainedModel):
        """
        应用MoE路由器Press到模型
        
        Args:
            model: 预训练模型
            
        Returns:
            context manager
        """
        if not isinstance(model, SUPPORTED_MODELS):
            logger.warning(f"Model {type(model)} not tested, supported models: {SUPPORTED_MODELS}")
        
        hooks = []
        
        try:
            # 根据模型类型选择不同的层访问方式
            if isinstance(model, GPT2LMHeadModel):
                # GPT2模型结构
                layers = model.transformer.h
                for i, layer in enumerate(layers):
                    layer.layer_idx = i
                    # 标记模型类型
                    layer.attn._model_type = 'gpt2'
                    # 注册到注意力层
                    hooks.append(layer.attn.register_forward_hook(self.forward_hook, with_kwargs=True))
            else:
                # 其他模型（Llama, Mistral等）
                layers = model.model.layers
                for i, layer in enumerate(layers):
                    layer.layer_idx = i
                    # 注册到注意力层
                    hooks.append(layer.self_attn.register_forward_hook(self.forward_hook, with_kwargs=True))
            
            yield
            
        finally:
            # 清理hooks
            for hook in hooks:
                hook.remove() 