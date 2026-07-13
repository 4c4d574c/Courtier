"""公文内容合规性校验模块。

基于 LLM + 规则库对公文正文内容进行合规性检查：
1. 文本领域分类（确定适用哪套规则）
2. 从规则库加载对应领域的校验规则
3. 调用 LLM 根据规则逐条验证，返回违规项

与 content_compliance 模块的关系：
- content_compliance/ 定义了检查器协议（ContentChecker Protocol）和通用注册机制。
- 本模块的 ContentChecker 实现该协议，可以被 CheckerRegistry 统一管理。
- 内部使用 Pydantic 模型（ContentViolation）解析 LLM 输出；对外通过适配方法
  check(text, subtype) 返回协议标准的 ComplianceResult。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from courtier.config import Settings as _Settings
from content_compliance.core import ComplianceResult, Violation
from courtier.db import AsyncDatabase, CRUDRepository
from courtier.db.tables import Rule, RuleDomain

logger = logging.getLogger(__name__)

# LLM defaults — empty sentinels at module level so that pydantic Settings
# validation (which reads .env) does not fire at import time.  Real defaults
# are resolved lazily inside ContentChecker.__init__ when params are empty.


# ======================== 数据模型 ========================


class TextClassification(BaseModel):
    """文本领域分类结果。"""

    domain_name: str = Field(description="领域名称（如：政务综合）")
    confidence: Literal["high", "medium", "low"] = Field(description="置信度")
    match_reason: str = Field(description="判定理由")


class ContentViolation(BaseModel):
    """单条内容违规结果。"""

    rule_id: str = Field(default="", description="规则编号")
    violat_dsc: str = Field(default="", description="违规描述")
    origin_text: str = Field(default="", description="原文摘录")
    severity: str = Field(default="", description="严重级别")
    cor_suggest: str = Field(default="", description="修正建议")


class ContentViolationList(BaseModel):
    """内容校验结果列表。"""

    results: list[ContentViolation] = Field(
        default_factory=list, description="违规结果列表"
    )


# ======================== Prompt 构建 ========================

_CLASSIFICATION_SYSTEM_PROMPT = """\
你是一个公文文本领域分类专家。

根据输入的公文正文内容，判断它属于以下哪个行政领域：
- 农业农村：农业、农村、农民、乡村振兴、粮食安全相关
- 审计监督：审计、督查、监察、廉政相关
- 文化旅游：文化、旅游、文物、体育、广播电视相关
- 生态环境：环境保护、生态建设、污染防治、气候变化相关
- 经济调控：宏观经济、产业政策、区域发展、统计相关
- 教育育才：教育、人才培养、学校、学术研究相关
- 应急管理：安全生产、防灾减灾、消防救援、突发事件相关
- 财政金融：财政、税收、金融、预算、债务相关
- 通用规范：跨领域通用规范、公文格式、行文规则相关
- 政务综合：综合性政务、行政事务、政府自身建设相关
- 卫生健康：医疗卫生、公共卫生、药品监管、计生相关
- 产业信息：工业、信息化、数字经济、通信相关
- 民生保障：民政、社保、就业、住房、养老、救助相关
- 资源规划：自然资源、国土规划、矿产资源、测绘地理相关
- 公共安全：公安、司法、国家安全、反恐、社会治安相关
- 科技创新：科技、创新、知识产权、高新技术企业相关
- 商贸流通：商贸、市场、物流、消费、外贸、口岸相关
- 交通运输：交通、公路、铁路、民航、水运、邮政相关
- 城乡建设：城市建设、市政、房地产、工程质量相关
- 水利防汛：水利、防汛抗旱、水资源、河湖管理相关

请以 JSON 格式输出，包含以下字段：
- domain_name: 领域名称（字符串，必须从上述列表中选取）
- confidence: 置信度（high / medium / low）
- match_reason: 判定理由（简述依据）
"""

_VALIDATION_SYSTEM_PROMPT = """\
你是一个公文内容合规审查专家。

根据给定规则逐条检查公文正文。对每条违规输出 JSON：
{"results": [{"rule_id": "规则编号", "violat_dsc": "违规定性(<=20字)", "origin_text": "违规原文摘录(<=30字)", "severity": "error|warning|info", "cor_suggest": "修正建议(<=20字)"}]}

无违规返回 {"results": []}"""


def _build_classification_prompt(text: str) -> str:
    """构建领域分类 prompt。"""
    # 截取前 2000 字避免 prompt 过长
    preview = text[:2000]
    return f"请对以下公文正文进行领域分类：\n\n{preview}"


MAX_PROMPT_LENGTH = 32000


def _build_validation_prompt(text: str, domain_name: str, rules: list[str]) -> str:
    """构建内容校验 prompt。"""
    rules_text = "\n\n".join(rules) if rules else "（无特定规则，请进行通用合规性检查）"
    prompt = (
        f"公文所属领域：{domain_name}\n\n校验规则：\n{rules_text}\n\n公文正文：\n{text}"
    )
    if len(prompt) > MAX_PROMPT_LENGTH:
        logger.warning(
            "Validation prompt exceeds MAX_PROMPT_LENGTH (%d chars), truncating",
            MAX_PROMPT_LENGTH,
        )
        prompt = prompt[:MAX_PROMPT_LENGTH]
    return prompt


# ======================== 文本分块 ========================


def _split_into_chunks(
    paragraphs: list[str],
    chunk_size: int = 15000,
    overlap: int = 1,
) -> list[str]:
    """将段落列表按字符数分块，块之间保留重叠段落。

    以段落为最小分块单位，不会在句子中间截断。
    相邻块共享 overlap 个段落作为上下文。

    Args:
        paragraphs: 段落文本列表。
        chunk_size: 每个块的目标字符数上限。
        overlap: 相邻块之间重叠的段落数。

    Returns:
        分块后的文本列表。
    """
    if not paragraphs:
        return []

    # 单个段落已超限，单独成块
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in paragraphs:
        if current_len + len(para) > chunk_size and current_parts:
            chunks.append("\n".join(current_parts))
            # 保留末尾 overlap 个段落作为下一块的上下文
            overlap_start = max(0, len(current_parts) - overlap)
            current_parts = current_parts[overlap_start:]
            current_len = sum(len(p) for p in current_parts)

        current_parts.append(para)
        current_len += len(para)

    if current_parts:
        chunks.append("\n".join(current_parts))

    return chunks


# ======================== LLM 调用工具 ========================


async def _call_llm_json(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    extra_body: dict[str, object] | None = None,
) -> dict:
    """调用 LLM 并解析 JSON 响应。

    Args:
        client: AsyncOpenAI 客户端
        model: 模型名称
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        temperature: 温度参数
        max_tokens: 最大输出 token 数
        extra_body: 额外的请求体参数（如 enable_thinking）

    Returns:
        解析后的 JSON 字典

    Raises:
        ValueError: 如果 LLM 返回空内容或无效 JSON
    """
    kwargs: dict[str, object] = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if extra_body is not None:
        kwargs["extra_body"] = extra_body

    response = await client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content
    logger.debug(
        "LLM 原始返回内容（max_tokens=%d, finish_reason=%s）：%s",
        max_tokens,
        response.choices[0].finish_reason,
        content[:500] if content else "<空>",
    )
    if not content:
        raise ValueError(
            f"LLM 返回空内容（finish_reason={response.choices[0].finish_reason}）"
        )

    # 尝试去掉 markdown 代码块包裹（部分模型即使指定 json_object 仍会包裹）
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 尝试解析，失败时尝试修复截断的 JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        repaired = _try_repair_truncated_json(content)
        if repaired is not None:
            logger.warning("JSON 被截断，已尝试修复")
            return repaired
        raise


def _try_repair_truncated_json(text: str) -> dict | None:
    """尝试修复被截断的 JSON 输出。

    常见场景：LLM 输出因 max_tokens 不足被截断，JSON 结构不完整。
    通过补充缺失的闭合括号尝试修复。

    NOTE: The json-repair library could replace this manual repair logic
    in the future (https://pypi.org/project/json-repair/).
    """
    if not text:
        return None

    # 补全截断的字符串值
    last_quote = text.rfind('"')
    # 如果引号数为奇数，去掉最后一个不完整的字符串字段值
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_string = not in_string
        i += 1
    if in_string:
        # 最后一个字符串未闭合，截断到上一个引号
        text = text[:last_quote] + '"'

    # 计数未闭合的括号并补齐
    open_count = text.count("{") - text.count("}")
    close_chars = "}" * open_count
    open_count = text.count("[") - text.count("]")
    close_chars += "]" * open_count

    text = text.rstrip(",\n\r\t ") + close_chars

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ======================== 内容校验器 ========================


class ContentChecker:
    """公文内容合规性校验器。

    工作流程：
    1. 对文本进行领域分类
    2. 从规则库加载该领域的校验规则
    3. 调用 LLM 逐条校验并返回违规项

    实现 content_compliance.ContentChecker 协议，可注册到 CheckerRegistry。
    doc_type 固定为 "通用"，因为本检查器通过 LLM 动态分类领域而非绑定单一文种。
    """

    # 本检查器不绑定单一文种，通过 LLM 动态分类领域
    _DOC_TYPE = "通用"

    @property
    def doc_type(self) -> str:
        """content_compliance.ContentChecker 协议要求的文种标识。"""
        return self._DOC_TYPE

    def __init__(
        self,
        db: AsyncDatabase | None = None,
        db_url: str | None = None,
        llm_base_url: str = "",
        llm_api_key: str = "",
        llm_model: str = "",
        temperature: float = 0.0,
        extra_body: dict[str, object] | None = None,
    ) -> None:
        # Resolve LLM defaults from Settings lazily so that pydantic
        # validation does not fire at module import time.
        s = _Settings()
        llm_base_url = llm_base_url or s.llm_base_url
        llm_api_key = llm_api_key or s.llm_api_key
        llm_model = llm_model or s.llm_model
        if temperature == 0.0:
            temperature = s.llm_temperature
        self._own_db = db is None
        if db is not None:
            self.db = db
        elif db_url:
            self.db = AsyncDatabase(db_url)
        else:
            self.db = AsyncDatabase(s.mysql_url)
        self._client = AsyncOpenAI(
            base_url=llm_base_url,
            api_key=llm_api_key,
        )
        self._model = llm_model
        self._temperature = temperature
        # Default to disabling Qwen thinking mode to avoid empty content responses.
        # The thinking tokens consume the max_tokens budget, leaving content empty.
        # vLLM may require chat_template_kwargs nesting to propagate to the template.
        self._extra_body: dict[str, object] = (
            extra_body if extra_body is not None
            else {"chat_template_kwargs": {"enable_thinking": False}}
        )

    async def __aenter__(self) -> "ContentChecker":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Dispose the database engine and HTTP client if created by this instance."""
        if self._own_db:
            await self.db.engine.dispose()
        await self._client.close()

    async def classify_text(self, text: str) -> TextClassification:
        """对文本进行领域分类。"""
        logger.info("文本分类请求已发送，文本长度：%d", len(text))

        user_prompt = _build_classification_prompt(text)
        raw = await _call_llm_json(
            self._client,
            self._model,
            _CLASSIFICATION_SYSTEM_PROMPT,
            user_prompt,
            temperature=self._temperature,
            max_tokens=1024,
            extra_body=self._extra_body,
        )

        logger.debug("分类 LLM 返回解析结果：%s", raw)

        if not isinstance(raw, dict):
            raise ValueError(
                f"LLM 返回了非预期的 JSON 类型 {type(raw).__name__}，"
                f"期望 object，实际: {str(raw)[:200]}"
            )

        result = TextClassification.model_validate(raw)
        logger.info(
            "文本分类结果：domain=%s, confidence=%s",
            result.domain_name,
            result.confidence,
        )
        return result

    async def _load_rules(
        self,
        domain_name: str,
        session: AsyncSession | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """从规则库加载指定领域的校验规则。

        Args:
            domain_name: 领域名称。
            session: 可选的已有数据库会话。若为 None 则从 self.db 创建新会话。

        Returns:
            (rules_text, rule_map): rules_text 是传给 LLM 的规则文本列表，
            rule_map 是 R{id} → name 的映射。
        """
        if session is not None:
            return await self._load_rules_with_session(domain_name, session)

        async with self.db.session() as session:
            return await self._load_rules_with_session(domain_name, session)

    async def _load_rules_with_session(
        self,
        domain_name: str,
        session: AsyncSession,
    ) -> tuple[list[str], dict[str, str]]:
        """Core rule-loading logic using a provided session."""
        rule_domain_repo = CRUDRepository(RuleDomain)
        rules_repo = CRUDRepository(Rule)

        domains = await rule_domain_repo.list(session, name=domain_name)
        if not domains:
            logger.warning("未找到领域 '%s' 的规则", domain_name)
            return [], {}

        domain = domains[0]
        logger.info("找到领域：id=%s, name=%s", domain.id, domain.name)

        rules = await rules_repo.list(session, domain_id=domain.id)

        logger.info("加载到 %d 条规则", len(rules))

        rule_map: dict[str, str] = {}
        rules_text: list[str] = []
        for rule in rules:
            key = f"R{rule.id}"
            rule_map[key] = rule.name or ""
            rules_text.append(f"{key}|{rule.description}|{rule.severity}")

        return rules_text, rule_map

    async def _process_chunk(self, chunk_text: str) -> list[dict]:
        """处理单个文本块：分类 → 加载规则 → 校验。

        每个块作为独立协程运行，块之间通过 asyncio.gather 并行。
        """
        # 1. 领域分类
        text_cls = await self.classify_text(chunk_text)

        # 2. 加载规则（共享 session 避免重复创建）
        async with self.db.session() as session:
            rules_text, rule_map = await self._load_rules(text_cls.domain_name, session)

        # 3. 调用 LLM 校验
        user_prompt = _build_validation_prompt(
            chunk_text,
            text_cls.domain_name,
            rules_text,
        )
        raw = await _call_llm_json(
            self._client,
            self._model,
            _VALIDATION_SYSTEM_PROMPT,
            user_prompt,
            temperature=self._temperature,
            max_tokens=4096,
            extra_body=self._extra_body,
        )

        if not isinstance(raw, dict):
            raise ValueError(
                f"LLM 校验返回了非预期的 JSON 类型 {type(raw).__name__}，"
                f"期望 object，实际: {str(raw)[:200]}"
            )

        val_result = ContentViolationList.model_validate(raw)
        logger.info(
            "块校验完成，domain=%s，发现 %d 条违规",
            text_cls.domain_name,
            len(val_result.results),
        )

        violations: list[dict] = []
        for item in val_result.results:
            v = {**item.model_dump(), "domain_name": text_cls.domain_name}
            rule_id = v.get("rule_id", "")
            v["rule_name"] = rule_map.get(rule_id, "")
            violations.append(v)

        return violations

    async def check_async(self, paragraphs: list[str]) -> list[dict]:
        """异步内容校验（流水线并行）。

        将段落分块后，每个块独立执行「分类 → 加载规则 → 校验」流程。
        所有块通过 asyncio.gather 并行运行，耗时 ≈ 单个块的耗时。

        Args:
            paragraphs: 公文正文段落列表。

        Returns:
            去重后的违规结果列表，每项包含 domain_name 字段。
        """
        chunks = _split_into_chunks(paragraphs)
        if not chunks:
            logger.warning("段落列表为空，跳过校验")
            return []

        logger.info(
            "开始流水线并行校验，%d 个段落 → %d 个块",
            len(paragraphs),
            len(chunks),
        )

        # 所有块并行执行，每个块内部串行（分类→校验）
        chunk_results = await asyncio.gather(
            *[self._process_chunk(chunk) for chunk in chunks],
            return_exceptions=True,
        )

        # 合并结果并按 origin_text 去重（重叠区域可能被两个块同时检出）
        all_violations: list[dict] = []
        seen_origins: set[str] = set()
        exceptions: list[Exception] = []

        for result in chunk_results:
            if isinstance(result, Exception):
                logger.error("块校验异常：%s", result)
                exceptions.append(result)
                continue
            for v in result:
                origin = v.get("origin_text", "")
                if origin and origin in seen_origins:
                    continue
                if origin:
                    seen_origins.add(origin)
                all_violations.append(v)

        # All chunks failed — raise instead of silently returning empty
        if exceptions and not all_violations and len(exceptions) == len(chunk_results):
            raise RuntimeError(f"所有 {len(chunks)} 个块校验均失败") from exceptions[0]

        # Some chunks failed — log warning so callers know results are partial
        if exceptions and all_violations:
            logger.warning(
                "部分块校验失败（%d/%d 个块），返回结果可能不完整",
                len(exceptions),
                len(chunk_results),
            )

        logger.info("校验完成，去重后共 %d 条违规", len(all_violations))
        return all_violations

    def _check_sync(self, paragraphs: list[str]) -> list[dict]:
        """同步内容校验（阻塞调用）。

        注意：如果在已有事件循环的上下文中（如 FastAPI），
        请直接使用 ``await checker.check_async(paragraphs)`` 而非此方法。

        Args:
            paragraphs: 公文正文段落列表。

        Returns:
            违规结果列表

        Raises:
            RuntimeError: 当在已有事件循环的上下文中调用此方法时。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("开始同步内容校验")
            return asyncio.run(self.check_async(paragraphs))
        else:
            raise RuntimeError(
                "_check_sync() 不能在运行中的事件循环内调用，"
                "请使用 await checker.check_async(paragraphs)"
            )

    # ======================== 协议适配 ========================

    async def check(
        self, text: str, subtype: str | None = None
    ) -> ComplianceResult:
        """content_compliance.ContentChecker 协议方法。

        对公文正文文本进行合规性检查。内部将文本按换行分段后
        调用 check_async 进行 LLM 校验，并将结果转换为协议标准格式。

        Args:
            text: 公文正文文本（段落以换行符分隔）。
            subtype: 可选子类型，本实现中保留以符合协议但暂未使用。

        Returns:
            ComplianceResult 包含协议标准的 Violation 列表。
        """
        paragraphs = [p for p in text.split("\n") if p.strip()]
        if not paragraphs:
            return ComplianceResult(is_valid=True, violations=())

        violations_raw = await self.check_async(paragraphs)
        violations = tuple(
            Violation(
                rule_id=v.get("rule_id", ""),
                message=v.get("violat_dsc", ""),
                severity=v.get("severity", "error"),
                position=v.get("origin_text", None),
            )
            for v in violations_raw
        )
        return ComplianceResult(
            is_valid=len(violations) == 0,
            violations=violations,
        )
