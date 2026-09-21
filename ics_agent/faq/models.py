# 模块职责：定义 FAQ 文档、快照、匹配结果和路由结果的数据模型。

from dataclasses import dataclass
from typing import Literal


Route = Literal["cache", "faq", "rag"]


@dataclass(frozen=True)
class FAQDocument:
    question_id: int
    question: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class FAQSnapshot:
    documents: tuple[FAQDocument, ...]

    @classmethod
    def from_lists(
        cls,
        question_ids: list[int],
        questions: list[str],
        tokenized_questions: list[list[str]],
    ) -> "FAQSnapshot":
        if not (
            len(question_ids)
            == len(questions)
            == len(tokenized_questions)
        ):
            raise ValueError(
                "FAQ question ids, questions, and tokenized questions "
                "must have the same length."
            )
        if not question_ids:
            raise ValueError("FAQ snapshot is empty.")

        documents = tuple(
            FAQDocument(
                question_id=int(question_id),
                question=str(question).strip(),
                tokens=tuple(
                    str(token).strip()
                    for token in tokens
                    if str(token).strip()
                ),
            )
            for question_id, question, tokens in zip(
                question_ids,
                questions,
                tokenized_questions,
            )
        )
        if not any(document.tokens for document in documents):
            raise ValueError("FAQ snapshot contains no usable tokens.")
        return cls(documents=documents)

    @property
    def question_ids(self) -> list[int]:
        return [document.question_id for document in self.documents]

    @property
    def questions(self) -> list[str]:
        return [document.question for document in self.documents]

    @property
    def tokenized_questions(self) -> list[list[str]]:
        return [list(document.tokens) for document in self.documents]


@dataclass(frozen=True)
class FAQMatch:
    document: FAQDocument
    bm25_score: float
    match_degree: float


@dataclass(frozen=True)
class FAQResult:
    route: Route
    query: str
    answer: str | None = None
    matched_question: str | None = None
    question_id: int | None = None
    bm25_score: float | None = None
    match_degree: float | None = None
    rag_query: str | None = None
    mysql_query: str | None = None
    cache_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "matched": self.route in {"cache", "faq"},
            "query": self.query,
            "answer": self.answer,
            "matched_question": self.matched_question,
            "question_id": self.question_id,
            "bm25_score": self.bm25_score,
            "match_degree": self.match_degree,
            "rag_query": self.rag_query,
            "mysql_query": self.mysql_query,
            "cache_key": self.cache_key,
        }
