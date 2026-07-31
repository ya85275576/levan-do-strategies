#!/usr/bin/env python3
"""
HighTempTation — 元控制器（第四波优化 #6）

功能:
  1. ContextualBandit  — Contextual Bandit（LinUCB）冷启动调度子策略
  2. PPOScheduler      — 简化 PPO（策略梯度 + clip 目标）在线调度
  3. ParticleFilterBMA — Particle Filter 在线贝叶斯模型平均（BMA）
  4. MetaController    — 三者融合: 输出最终子策略调度 + 置信度 + 权重

用法:
  from highopt_ultra.metacontroller import (
      ContextualBandit, PPOScheduler, ParticleFilterBMA, MetaController,
  )

  # 子策略 = 不同的开仓规则变体（如: edge 阈值 / 深度过滤 / 时段过滤）
  STRATEGIES = ["edge_0.15", "edge_0.20", "depth_strict", "time_window"]

  bandit = ContextualBandit(n_arms=len(STRATEGIES), dim=6)
  arm = bandit.select(context_vec)               # LinUCB 选择
  bandit.update(arm, context_vec, reward=0.05)

  ppo = PPOScheduler(dim=6, n_actions=len(STRATEGIES))
  act = ppo.act(context_vec)                     # 策略采样
  ppo.learn(states, actions, rewards)            # 轨迹学习

  bma = ParticleFilterBMA(n_arms=len(STRATEGIES), n_particles=200)
  bma.observe(rewards_vec)                       # 每轮喂各臂收益
  w = bma.posterior_weights()                    # 后验权重

  meta = MetaController(strategies=STRATEGIES)
  decision = meta.decide(context_vec, recent_rewards)
  # → {"strategy": "edge_0.20", "confidence": 0.72, "weights": {...}, "arms": {...}}
"""
import logging
import math
import random
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("highopt_ultra.metacontroller")


# ════════════════════════════════════════════════════════════════
# 1. Contextual Bandit — LinUCB
# ════════════════════════════════════════════════════════════════

class ContextualBandit:
    """
    LinUCB（上下文赌博机）。

    每臂维护岭回归参数: A = X'X + λI, b = X'r, θ = A⁻¹b。
    UCB 上界 = θᵀx + α·√(xᵀA⁻¹x)。以最高上界选择臂。

    上下文建议（6 维）:
      [edge, 1-depth_norm, 时段正弦, 波动率, 城市数, 近期收益]
    """

    def __init__(self, n_arms: int, dim: int, alpha: float = 1.0,
                 lam: float = 1.0, seed: int = 0):
        self.n_arms = n_arms
        self.dim = dim
        self.alpha = alpha
        self.lam = lam
        self._rng = np.random.default_rng(seed)
        self.A = np.stack([lam * np.eye(dim) for _ in range(n_arms)])
        self.b = np.zeros((n_arms, dim))
        self.counts = np.zeros(n_arms)
        self._Ainv_cache: Dict[int, np.ndarray] = {}

    def _theta(self, arm: int) -> np.ndarray:
        return np.linalg.solve(self.A[arm], self.b[arm])

    def select(self, context: np.ndarray, return_ucb: bool = False):
        """返回最优臂（可附带 UCB 值）"""
        x = np.asarray(context, dtype=float)
        ucbs = np.zeros(self.n_arms)
        for a in range(self.n_arms):
            Ainv = np.linalg.inv(self.A[a])
            self._Ainv_cache[a] = Ainv
            theta = self._theta(a)
            ucbs[a] = theta @ x + self.alpha * math.sqrt(max(0.0, x @ Ainv @ x))
        arm = int(np.argmax(ucbs))
        return (arm, float(ucbs[arm])) if return_ucb else arm

    def update(self, arm: int, context: np.ndarray, reward: float):
        x = np.asarray(context, dtype=float)
        self.A[arm] += np.outer(x, x)
        self.b[arm] += reward * x
        self.counts[arm] += 1
        self._Ainv_cache.pop(arm, None)

    def arm_weights(self) -> np.ndarray:
        """各臂当前偏好（softmax of 平均收益近似）"""
        avg = np.array([self.b[a] @ self._theta(a) if self.counts[a] else 0.0
                        for a in range(self.n_arms)])
        avg = np.clip(avg, -5, 5)
        e = np.exp(avg - avg.max())
        return e / e.sum()


# ════════════════════════════════════════════════════════════════
# 2. 简化 PPO 调度器
# ════════════════════════════════════════════════════════════════

class PPOScheduler:
    """
    简化 PPO（Proximal Policy Optimization）。

    策略: 单隐层 MLP(dim → h → n_actions) + softmax（分类策略）。
    目标: L = -E[min(r·A, clip(r,1-ε,1+ε)·A)]  (r = π(a)/π_old(a))
    优化: 数值梯度（参数少, 免去手推反向传播, 自检稳定）。
    基线: 近期平均奖励（替代完整 critic/GAE, 简化实现）。

    用法:
      ppo = PPOScheduler(dim=6, n_actions=4, hidden=8)
      a, prob = ppo.act(context)                    # 采样动作
      ppo.learn(states, actions, rewards, old_probs)  # PPO 更新
      p = ppo.probs(context)                        # 动作分布
    """

    def __init__(self, dim: int, n_actions: int, hidden: int = 8,
                 clip_eps: float = 0.2, lr: float = 0.05, seed: int = 0):
        self.dim = dim
        self.n_actions = n_actions
        self.hidden = hidden
        self.clip_eps = clip_eps
        self.lr = lr
        self._rng = np.random.default_rng(seed)
        # 参数向量: W1(d×h), b1(h), W2(h×K), b2(K)
        self._n_w1 = dim * hidden
        self._n_b1 = hidden
        self._n_w2 = hidden * n_actions
        self._n_b2 = n_actions
        self._total = self._n_w1 + self._n_b1 + self._n_w2 + self._n_b2
        self._theta = np.zeros(self._total)
        # He 初始化
        w1 = self._rng.normal(0, math.sqrt(2.0 / dim), (dim, hidden))
        w2 = self._rng.normal(0, math.sqrt(2.0 / hidden), (hidden, n_actions))
        self._set_params(w1, np.zeros(hidden), w2, np.zeros(n_actions))
        self.history: List[dict] = []
        self._rewards_ewma = 0.0

    # ── 参数打包/解包 ──
    def _get_params(self):
        p = self._theta
        w1 = p[:self._n_w1].reshape(self.dim, self.hidden)
        b1 = p[self._n_w1:self._n_w1 + self._n_b1]
        w2 = p[self._n_w1 + self._n_b1:self._n_w1 + self._n_b1 + self._n_w2] \
             .reshape(self.hidden, self.n_actions)
        b2 = p[self._n_w1 + self._n_b1 + self._n_w2:]
        return w1, b1, w2, b2

    def _set_params(self, w1, b1, w2, b2):
        self._theta[:self._n_w1] = w1.ravel()
        self._theta[self._n_w1:self._n_w1 + self._n_b1] = b1
        self._theta[self._n_w1 + self._n_b1:self._n_w1 + self._n_b1 + self._n_w2] = w2.ravel()
        self._theta[self._n_w1 + self._n_b1 + self._n_w2:] = b2

    # ── 前向 ──
    def forward(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """X (N,d) → (probs (N,K), logits)"""
        w1, b1, w2, b2 = self._get_params()
        h = np.tanh(X @ w1 + b1)
        logits = h @ w2 + b2
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True), logits

    def probs(self, context: np.ndarray) -> np.ndarray:
        p, _ = self.forward(np.atleast_2d(np.asarray(context, dtype=float)))
        return p[0]

    def act(self, context: np.ndarray, return_prob: bool = False):
        """采样一个动作"""
        p = self.probs(context)
        action = int(self._rng.choice(self.n_actions, p=p))
        return (action, float(p[action])) if return_prob else action

    # ── PPO 损失 ──
    def _loss(self, states: np.ndarray, actions: np.ndarray,
              advantages: np.ndarray, old_probs: np.ndarray) -> float:
        probs, _ = self.forward(states)
        new_pa = probs[np.arange(len(actions)), actions]
        ratio = new_pa / np.maximum(old_probs, 1e-12)
        clipped = np.clip(ratio, 1 - self.clip_eps, 1 + self.clip_eps)
        surr = np.minimum(ratio * advantages, clipped * advantages)
        return -float(np.mean(surr))

    def learn(self, states: np.ndarray, actions: np.ndarray,
              rewards: np.ndarray, old_probs: np.ndarray,
              gamma: float = 0.98) -> float:
        """PPO 更新一步, 返回 loss 下降量"""
        states = np.atleast_2d(np.asarray(states, dtype=float))
        actions = np.asarray(actions, dtype=int)
        rewards = np.asarray(rewards, dtype=float)
        old_probs = np.asarray(old_probs, dtype=float)
        if self._rewards_ewma == 0.0:
            self._rewards_ewma = float(rewards.mean())
        else:
            self._rewards_ewma = 0.9 * self._rewards_ewma + 0.1 * float(rewards.mean())
        advantages = rewards - self._rewards_ewma       # 简化基线

        loss0 = self._loss(states, actions, advantages, old_probs)
        # 数值梯度
        eps = 1e-4
        grad = np.zeros_like(self._theta)
        for i in range(self._total):
            orig = self._theta[i]
            self._theta[i] = orig + eps
            lp = self._loss(states, actions, advantages, old_probs)
            self._theta[i] = orig - eps
            lm = self._loss(states, actions, advantages, old_probs)
            self._theta[i] = orig
            grad[i] = (lp - lm) / (2 * eps)
        self._theta -= self.lr * grad
        loss1 = self._loss(states, actions, advantages, old_probs)
        self.history.append({"loss0": loss0, "loss1": loss1, "advantage_mean":
                             float(advantages.mean())})
        return loss0 - loss1


# ════════════════════════════════════════════════════════════════
# 3. Particle Filter 在线 BMA
# ════════════════════════════════════════════════════════════════

class ParticleFilterBMA:
    """
    Particle Filter 在线贝叶斯模型平均（BMA）。

    每个粒子 = 各臂权重向量 w（softmax 归一化）。每轮观测各臂收益
    向量 r, 似然 = exp(β · w·r)（加权收益越高的粒子权重越大）,
    再系统重采样 + 噪声注入, 得到后验权重分布。

    用法:
      bma = ParticleFilterBMA(n_arms=4, n_particles=300, beta=3.0)
      for _ in range(50):
          bma.observe(rewards_vec)       # rewards_vec = 各臂本轮收益
      w = bma.posterior_weights()        # → 归一化权重
      eff = bma.effective_sample_size()  # 粒子多样性
    """

    def __init__(self, n_arms: int, n_particles: int = 300,
                 beta: float = 3.0, noise: float = 0.05, seed: int = 0):
        self.n_arms = n_arms
        self.n_particles = n_particles
        self.beta = beta
        self.noise = noise
        self._rng = np.random.default_rng(seed)
        # 粒子: 从 Dirichlet(1) 采样权重（均匀先验）
        self.particles = self._rng.dirichlet(np.ones(n_arms), n_particles)
        self.weights = np.full(n_particles, 1.0 / n_particles)

    def observe(self, rewards: np.ndarray):
        """喂入各臂本轮收益向量 (n_arms,)"""
        r = np.asarray(rewards, dtype=float)
        logw = self.beta * (self.particles @ r)          # 加权收益
        logw -= logw.max()
        w = np.exp(logw)
        self.weights = w / w.sum()
        self._resample()

    def _resample(self):
        """系统重采样 + 噪声注入"""
        cdf = np.cumsum(self.weights)
        u = (np.arange(self.n_particles) + self._rng.uniform(0, 1)) / self.n_particles
        idx = np.searchsorted(cdf, u)
        self.particles = self.particles[idx]
        self.particles += self._rng.normal(0, self.noise, self.particles.shape)
        self.particles = np.abs(self.particles) + 1e-9
        self.particles /= self.particles.sum(axis=1, keepdims=True)
        self.weights = np.full(self.n_particles, 1.0 / self.n_particles)

    def posterior_weights(self) -> np.ndarray:
        return self.particles.mean(axis=0)

    def effective_sample_size(self) -> float:
        return 1.0 / np.sum(self.weights ** 2)


# ════════════════════════════════════════════════════════════════
# 4. 元控制器（融合）
# ════════════════════════════════════════════════════════════════

class MetaController:
    """
    元控制器 — 统一调度子策略。

    融合三路信号:
      - LinUCB:       探索/利用冷启动
      - PPO:          策略网络在线优化
      - Particle BMA: 后验权重贝叶斯平均
    投票权重: bandit 0.3 / ppo 0.4 / bma 0.3（可配）。
    若三路分歧大 → 置信度低 → 建议降级到最保守策略。

    用法:
      meta = MetaController(strategies=["edge_0.15", "edge_0.20", "depth_strict"])
      decision = meta.decide(context, recent_rewards)
    """

    def __init__(self, strategies: List[str], dim: int = 6,
                 vote: Optional[Dict[str, float]] = None, seed: int = 0):
        self.strategies = list(strategies)
        self.n = len(self.strategies)
        self.vote = vote or {"bandit": 0.3, "ppo": 0.4, "bma": 0.3}
        self.bandit = ContextualBandit(n_arms=self.n, dim=dim, seed=seed)
        self.ppo = PPOScheduler(dim=dim, n_actions=self.n, seed=seed)
        self.bma = ParticleFilterBMA(n_arms=self.n, seed=seed)
        self.decisions: List[dict] = []

    def decide(self, context: np.ndarray, recent_rewards: np.ndarray,
               learn: bool = True) -> dict:
        x = np.asarray(context, dtype=float)

        arm_b, ucb = self.bandit.select(x, return_ucb=True)
        arm_p, prob_p = self.ppo.act(x, return_prob=True)
        self.bma.observe(np.asarray(recent_rewards, dtype=float))
        w_bma = self.bma.posterior_weights()
        arm_m = int(np.argmax(w_bma))

        # 加权投票
        score = np.zeros(self.n)
        score[arm_b] += self.vote["bandit"]
        score[arm_p] += self.vote["ppo"]
        score += self.vote["bma"] * w_bma
        final = int(np.argmax(score))
        confidence = float(score[final])

        # 分歧度 → 置信度修正
        arms = {arm_b, arm_p, arm_m}
        agreement = 3 - len(arms)           # 3 路全一致 = 2
        confidence *= (0.5 + 0.25 * agreement)

        decision = {
            "ts": time.time(),
            "strategy": self.strategies[final],
            "confidence": round(confidence, 3),
            "weights": {s: round(float(w), 4) for s, w in zip(self.strategies, w_bma)},
            "arms": {"bandit": self.strategies[arm_b], "ppo": self.strategies[arm_p],
                     "bma": self.strategies[arm_m]},
        }
        self.decisions.append(decision)

        if learn:
            # 在线学习: 当前最优臂收益作为反馈
            best_rew = float(np.asarray(recent_rewards)[final])
            self.bandit.update(final, x, best_rew)
            self.ppo.learn(np.atleast_2d(x), np.array([final]),
                           np.array([best_rew]),
                           np.array([prob_p if arm_p == final else
                                     max(self.ppo.probs(x)[final], 1e-6)]))
        return decision

    def summary(self) -> dict:
        return {
            "n_decisions": len(self.decisions),
            "last": self.decisions[-1] if self.decisions else None,
            "bandit_counts": self.bandit.counts.tolist(),
            "bma_weights": self.bma.posterior_weights().tolist(),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    STRATS = ["edge_0.15", "edge_0.20", "depth_strict", "time_window"]
    meta = MetaController(strategies=STRATS, dim=6, seed=3)
    rng = np.random.default_rng(1)
    for i in range(40):
        ctx = rng.normal(0, 1, 6)
        # 臂 1（edge_0.20）真实最优
        rew = np.array([0.01, 0.05, -0.01, 0.00]) + rng.normal(0, 0.01, 4)
        d = meta.decide(ctx, rew)
        if i % 10 == 0:
            print(f"step {i}: strategy={d['strategy']} conf={d['confidence']}")
    print("summary:", meta.summary())
    print("ppo loss history tail:", meta.ppo.history[-1])
