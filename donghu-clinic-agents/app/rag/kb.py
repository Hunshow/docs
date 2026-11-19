# -*- coding: utf-8 -*-
"""RAG 知识库模块：文档分块 + BM25 检索（jieba 分词）+ 知识图谱扩展 + 可选 Ollama 语义向量。"""  # 模块职责说明
import json                                              # 索引持久化
import math                                              # BM25 计算用对数
import re                                                # 文本清洗
from pathlib import Path                                 # 路径处理
from typing import Dict, List, Optional, Tuple           # 类型标注
import jieba                                             # 中文分词（依赖清单内）
from app.config import get_settings                      # 统一配置
from app.rag.graph import KnowledgeGraph                 # 知识图谱

_BM25_K1 = 1.5                                           # BM25 饱和参数 k1（经验值）
_BM25_B = 0.75                                           # BM25 长度归一参数 b（经验值）
_STOPWORDS = {"的", "了", "是", "在", "和", "与", "或", "对", "为", "及",  # 停用词表
              "并", "要", "需", "将", "把", "被", "有", "无", "不", "等"}  # 检索前剔除


class KnowledgeBase:                                     # 知识库类
    """RAG 知识库：加载 data/kb/*.md 分块建索引，检索返回带来源的参考片段。"""

    def __init__(self) -> None:                          # 构造：初始化容器
        """初始化知识库容器。入参无；出参实例；失败行为无。"""
        self.chunks: List[Dict] = []                     # 分块列表：{doc, chunk_id, text, tokens}
        self.df: Dict[str, int] = {}                     # 词文档频率：BM25 的 IDF 基础
        self.avgdl: float = 0.0                          # 平均块长：BM25 归一化
        self.graph = KnowledgeGraph()                    # 挂载知识图谱
        self._built = False                              # 索引构建标志

    # ---------- 路径与加载 ----------

    def _docs_dir(self) -> Path:                         # 内部工具：文档目录
        """计算知识库文档目录绝对路径。入参无；出参 Path；失败行为无。"""
        rel = get_settings().rag.get("docs_dir", "data/kb")  # 读配置
        return Path(__file__).resolve().parent.parent.parent / rel  # 项目根拼接

    def _index_path(self) -> Path:                       # 内部工具：索引文件路径
        """计算检索索引文件绝对路径。入参无；出参 Path；失败行为无。"""
        rel = get_settings().rag.get("index_path", "data/kb/index.json")  # 读配置
        return Path(__file__).resolve().parent.parent.parent / rel  # 项目根拼接

    def _tokenize(self, text: str) -> List[str]:         # 内部工具：中文分词
        """jieba 分词 + 停用词过滤 + 小写归一。

        入参：text 原始文本。出参：词元列表。
        失败行为：空文本返回空列表。
        """
        text = re.sub(r"\s+", " ", str(text)).strip()    # 压缩空白
        if not text:                                     # 空文本
            return []                                    # 空结果
        words = jieba.lcut(text)                         # jieba 切词
        return [w.lower() for w in words                 # 归一小写
                if w.strip() and w not in _STOPWORDS     # 去停用词
                and not re.fullmatch(r"[，。、；：！？\s\d\W]", w)]  # 去纯标点/数字

    # ---------- 索引构建 ----------

    def build(self, force: bool = False) -> int:         # 构建索引
        """读取 data/kb/*.md 全部文档，分块并建立 BM25 索引，同时构建知识图谱。

        入参：force 为 True 时强制重建（默认有索引缓存则跳过）。
        出参：分块数量 int。
        失败行为：目录不存在或无文档时返回 0（系统仍可启动，检索降级为空）。
        """
        if self._built and not force:                    # 已构建且未强制
            return len(self.chunks)                      # 直接返回现量
        docs_dir = self._docs_dir()                      # 取文档目录
        self.chunks = []                                 # 清空旧分块
        self.df = {}                                     # 清空旧词频
        if docs_dir.exists():                            # 目录存在才处理
            for md in sorted(docs_dir.glob("*.md")):     # 逐个 Markdown 文档
                try:                                     # 单文档损坏不影响整体
                    text = md.read_text(encoding="utf-8")  # 读文档
                except OSError:                          # 读失败
                    continue                             # 跳过该文档
                doc_name = md.stem                       # 文档名（作为溯源来源）
                for seg in re.split(r"\n#{2,3}\s", text):  # 按 ##/### 标题分块（制度文档天然分段）
                    seg = seg.strip()                    # 清理空白
                    if len(seg) < 10:                    # 过短片段无检索价值
                        continue                         # 跳过
                    tokens = self._tokenize(seg)         # 分词
                    if not tokens:                       # 分词后为空
                        continue                         # 跳过
                    self.chunks.append({"doc": doc_name, "text": seg[:500],  # 截断到 500 字防超长
                                        "tokens": tokens})  # 入块列表
        # 统计词文档频率（BM25 IDF 基础）
        total_len = 0                                    # 总长度累加器
        for c in self.chunks:                            # 遍历分块
            seen = set(c["tokens"])                      # 块内去重词
            for w in seen:                               # 逐词
                self.df[w] = self.df.get(w, 0) + 1       # 文档频率 +1
            total_len += len(c["tokens"])                # 累加块长
        self.avgdl = (total_len / len(self.chunks)) if self.chunks else 0.0  # 平均块长
        self._build_graph(docs_dir)                      # 构建知识图谱
        self._built = True                               # 标记已构建
        self._save_index()                               # 持久化索引（可选成功与否不阻断）
        return len(self.chunks)                          # 返回分块数

    def _save_index(self) -> bool:                       # 内部工具：持久化索引
        """将分块（不含词元）与图谱写入磁盘。入参无；出参成功与否；失败行为：异常吞掉不阻断。"""
        try:                                             # 磁盘容错
            p = self._index_path()                       # 取索引路径
            p.parent.mkdir(parents=True, exist_ok=True)  # 确保目录
            slim = [{"doc": c["doc"], "text": c["text"]} for c in self.chunks]  # 瘦身：去掉词元
            with open(p, "w", encoding="utf-8") as f:    # 写索引
                json.dump(slim, f, ensure_ascii=False, indent=2)  # 序列化
            return self.graph.save()                     # 顺带持久化图谱
        except OSError:                                  # 磁盘异常
            return False                                 # 静默失败（索引可重建）

    def _build_graph(self, docs_dir: Path) -> None:      # 内部工具：构建知识图谱
        """从 rules_triples.json（若有）与文档标题实体构建三元组图谱。

        入参：docs_dir 文档目录。出参：无（填充 self.graph）。
        失败行为：三元组定义文件缺失/损坏时跳过，仅用文档实体建图。
        """
        self.graph = KnowledgeGraph()                    # 重置图谱
        tri_file = docs_dir / "rules_triples.json"       # 预定义三元组文件（人工整理的规则网络）
        if tri_file.exists():                            # 文件存在
            try:                                         # 容错解析
                rows = json.loads(tri_file.read_text(encoding="utf-8"))  # 解析
                for r in rows:                           # 逐条入图
                    self.graph.add_triple(str(r.get("s", "")), str(r.get("p", "")),  # 主体/关系
                                          str(r.get("o", "")), source=str(r.get("source", "-")))  # 客体/来源
            except (json.JSONDecodeError, OSError):      # 损坏处理
                pass                                     # 跳过预定义三元组
        for c in self.chunks:                            # 从分块标题补充实体节点
            first_line = c["text"].split("\n")[0][:40]   # 取块首行作为实体近似
            if first_line:                               # 非空才建
                self.graph.add_triple(c["doc"], "包含规则", first_line, source=c["doc"])  # 文档→规则边

    # ---------- 检索 ----------

    def _bm25_score(self, query_tokens: List[str], chunk: Dict) -> float:  # 内部工具：单块 BM25 打分
        """计算查询与单个分块的 BM25 得分。

        入参：query_tokens 查询词元；chunk 分块。
        出参：float 得分。
        失败行为：空查询或空块返回 0。
        """
        if not query_tokens or not chunk.get("tokens"):  # 空输入
            return 0.0                                   # 零分
        n = max(len(self.chunks), 1)                     # 总块数（防除零）
        dl = len(chunk["tokens"])                        # 块长
        score = 0.0                                      # 得分累加器
        tf: Dict[str, int] = {}                          # 块内词频
        for w in chunk["tokens"]:                        # 统计词频
            tf[w] = tf.get(w, 0) + 1                     # 累加
        for q in set(query_tokens):                      # 逐查询词
            if q not in tf:                              # 块内无该词
                continue                                 # 跳过
            df = self.df.get(q, 0)                       # 文档频率
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))  # BM25 IDF（平滑）
            denom = tf[q] + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / max(self.avgdl, 1.0))  # 归一分母
            score += idf * (tf[q] * (_BM25_K1 + 1)) / denom  # 累加得分
        return score                                     # 返回得分

    def retrieve(self, query: str, top_k: Optional[int] = None) -> Dict:  # RAG 检索主入口
        """检索与查询最相关的知识片段 + 图谱关联三元组。

        入参：query 自然语言查询；top_k 返回条数（默认取配置）。
        出参：{"references": [{"doc","text","score"}], "graph_triples": [...], "graph_entities": [...]}。
        失败行为：索引未建时自动 build；无命中返回空 references（调用方据此降级）。
        """
        if not self._built:                              # 惰性构建
            self.build()                                 # 首次调用建索引
        k = top_k or int(get_settings().rag.get("top_k", 4))  # 读取返回条数
        q_tokens = self._tokenize(query)                 # 查询分词
        scored: List[Tuple[float, Dict]] = []            # (得分, 分块) 列表
        for c in self.chunks:                            # 遍历分块打分
            s = self._bm25_score(q_tokens, c)            # BM25 得分
            if s > 0:                                    # 仅保留有得分的
                scored.append((s, c))                    # 收集
        scored.sort(key=lambda x: x[0], reverse=True)    # 按得分降序
        refs = [{"doc": c["doc"], "text": c["text"], "score": round(s, 4)}  # 组装引用
                for s, c in scored[:k]]                  # 取 top_k
        # 图谱扩展：用查询关键词匹配三元组，并从命中实体做一步邻域扩展
        graph_triples = self.graph.match_triples(q_tokens)  # 直接匹配
        entities: List[str] = []                         # 扩展实体容器
        for t in graph_triples:                          # 从命中三元组取实体
            for side in ("s", "o"):                      # 主体与客体
                e = str(t.get(side, ""))                 # 取实体
                if e and e not in entities:              # 去重收集
                    entities.append(e)                   # 记录
        for e in entities[:3]:                           # 对前 3 个实体扩展（控制成本）
            for rel in self.graph.expand(e, depth=1):    # 一跳邻居
                if rel not in entities:                  # 去重
                    entities.append(rel)                 # 收集扩展实体
        return {"references": refs,                      # 知识片段引用
                "graph_triples": graph_triples,          # 命中三元组
                "graph_entities": entities[:10]}         # 扩展实体（上限 10）

# 进程级单例：全项目共享同一份索引，避免重复建索引
_KB_SINGLETON: Optional[KnowledgeBase] = None            # 单例容器

def get_kb() -> KnowledgeBase:                           # 获取知识库单例
    """获取进程级知识库单例（首次调用自动建索引）。

    入参：无。出参：KnowledgeBase 实例。
    失败行为：建索引失败时返回空索引实例（检索为空但不抛异常），保证服务可用性。
    """
    global _KB_SINGLETON                                 # 声明使用全局
    if _KB_SINGLETON is None:                            # 尚未创建
        _KB_SINGLETON = KnowledgeBase()                  # 创建
        _KB_SINGLETON.build()                            # 立即建索引
    return _KB_SINGLETON                                 # 返回
