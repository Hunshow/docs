# -*- coding: utf-8 -*-
"""知识图谱模块：以三元组（主体-关系-客体）存图，支持邻域扩展，为 RAG 提供可溯源的规则网络。"""  # 模块职责说明
import json                                              # 持久化序列化
import itertools                                         # 组合遍历（共现建边用）
from pathlib import Path                                 # 路径处理
from typing import Dict, List, Optional, Set, Tuple      # 类型标注
from app.config import get_settings                      # 统一配置


class KnowledgeGraph:                                    # 知识图谱类
    """轻量知识图谱：三元组列表 + 邻接索引，JSON 持久化，纯标准库实现（不引入图数据库）。"""

    def __init__(self) -> None:                          # 构造：初始化容器
        """初始化空图谱。

        入参：无。出参：实例（triples 空、索引空、未加载状态）。
        失败行为：无。
        """
        self.triples: List[Dict] = []                    # 三元组列表：{s,p,o,source}
        self._adj: Dict[str, Set[str]] = {}              # 邻接索引：实体 → 关联实体集合
        self._loaded = False                             # 加载标志

    def _graph_path(self) -> Path:                       # 内部工具：图谱文件路径
        """计算图谱持久化文件绝对路径。入参无；出参 Path；失败行为无。"""
        rel = get_settings().rag.get("graph_path", "data/kb/knowledge_graph.json")  # 读配置
        return Path(__file__).resolve().parent.parent.parent / rel  # 项目根拼接

    def load(self) -> None:                              # 加载图谱
        """从 JSON 文件加载三元组并重建邻接索引。

        入参：无。出参：无（填充 self.triples 与 self._adj）。
        失败行为：文件不存在则保持空图；文件损坏则回退空图并保留原文件为 .bak。
        """
        if self._loaded:                                 # 幂等：已加载不重复
            return                                       # 直接返回
        p = self._graph_path()                           # 取路径
        if p.exists():                                   # 文件存在
            try:                                         # 容错解析
                with open(p, "r", encoding="utf-8") as f:  # 读文件
                    self.triples = json.load(f)          # 解析三元组列表
            except (json.JSONDecodeError, OSError):      # 损坏处理
                p.rename(p.with_suffix(".json.bak"))     # 保留损坏文件备查
                self.triples = []                        # 回退空图
        for t in self.triples:                           # 重建邻接索引
            self._link(t.get("s", ""), t.get("o", ""))   # 双向建边
        self._loaded = True                              # 标记已加载

    def _link(self, a: str, b: str) -> None:             # 内部工具：建立双向邻接
        """在邻接索引中建立 a↔b 双向关联。入参两个实体；出参无；失败行为：空串跳过。"""
        if not a or not b:                               # 空实体不建边
            return                                       # 跳过
        self._adj.setdefault(a, set()).add(b)            # a→b
        self._adj.setdefault(b, set()).add(a)            # b→a

    def add_triple(self, s: str, p: str, o: str, source: str = "-") -> bool:  # 新增三元组
        """新增一条三元组（去重）并更新索引。

        入参：s 主体；p 关系；o 客体；source 来源文档标识（溯源用）。
        出参：True 新增成功 / False 已存在（幂等）。
        失败行为：任一要素为空返回 False，不入图——残缺三元组会污染检索。
        """
        if not s or not p or not o:                      # 完整性校验
            return False                                 # 拒绝空要素
        for t in self.triples:                           # 去重检查
            if (t.get("s"), t.get("p"), t.get("o")) == (s, p, o):  # 完全相同
                return False                             # 幂等返回
        tri = {"s": s, "p": p, "o": o, "source": source}  # 组装三元组（含来源：审计可溯源）
        self.triples.append(tri)                         # 入列表
        self._link(s, o)                                 # 更新邻接
        return True                                      # 成功

    def save(self) -> bool:                              # 持久化图谱
        """将三元组写入 JSON 文件。

        入参：无。出参：True 成功 / False 失败。
        失败行为：磁盘异常返回 False。
        """
        try:                                             # 磁盘容错
            p = self._graph_path()                       # 取路径
            p.parent.mkdir(parents=True, exist_ok=True)  # 确保目录
            with open(p, "w", encoding="utf-8") as f:    # 覆盖写（图谱由代码重建，允许覆盖）
                json.dump(self.triples, f, ensure_ascii=False, indent=2)  # 序列化
            return True                                  # 成功
        except OSError:                                  # 磁盘异常
            return False                                 # 失败

    def match_triples(self, keywords: List[str], limit: int = 8) -> List[Dict]:  # 关键词匹配三元组
        """按关键词匹配三元组：任一要素命中即返回。

        入参：keywords 关键词列表；limit 最大返回数。
        出参：命中三元组列表（含 source 溯源字段）。
        失败行为：无命中返回空列表。
        """
        hits: List[Dict] = []                            # 命中容器
        kws = [k for k in keywords if k]                 # 去空词
        for t in self.triples:                           # 遍历三元组
            text = f"{t.get('s','')}{t.get('p','')}{t.get('o','')}"  # 拼接全文
            if any(k in text for k in kws):              # 任一关键词命中
                hits.append(t)                           # 收集
                if len(hits) >= limit:                   # 达到上限
                    break                                # 提前结束
        return hits                                      # 返回命中

    def expand(self, entity: str, depth: int = 1) -> List[str]:  # 邻域扩展
        """从实体出发做邻域扩展，返回关联实体（规则关联发现用）。

        入参：entity 起始实体；depth 扩展深度（演示图规模用 1 层即可）。
        出参：关联实体列表（不含起始实体）。
        失败行为：未知实体返回空列表。
        """
        if not entity or entity not in self._adj:        # 未知实体
            return []                                    # 空结果
        result: Set[str] = set(self._adj[entity])        # 一层邻居
        if depth >= 2:                                   # 深层扩展
            frontier = list(result)                      # 当前层快照
            for e in frontier:                           # 逐个二跳
                result |= self._adj.get(e, set())        # 并入二跳邻居
            result.discard(entity)                       # 移除自身
        return sorted(result)                            # 排序返回（稳定输出）
