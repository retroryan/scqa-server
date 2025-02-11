"""Server."""
import logging.config
import os
import random
import time
import traceback
from enum import IntEnum
from typing import Awaitable
from typing import Callable

import boto3  # type: ignore
import openai
import pinecone  # type: ignore
from dotenv import load_dotenv
from fastapi import BackgroundTasks
from fastapi import FastAPI
from fastapi import Query
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from server.search_utils import get_prompt
from server.search_utils import log_metrics


# init environment
load_dotenv()
pinecone_key = os.environ["PINECONE_KEY"]
pinecone_env = os.environ["PINECONE_ENV"]
openai_key = os.environ["OPENAI_KEY"]

# init logging
logging.config.fileConfig("server/logging.ini")
logger = logging.getLogger(__name__)

# init fastapi
debug = os.environ.get("DEBUG", "false").lower() == "true"
app = FastAPI(debug=debug)

# init openai
openai.api_key = openai_key
embedding_model = "text-embedding-ada-002"
prompt_limit = 3000  # 3750

# init pinecone
index_name = "scqa"
# initialize connection to pinecone (get API key at app.pinecone.io)
pinecone.init(
    api_key=pinecone_key,
    environment=pinecone_env,
)
index = pinecone.Index(index_name)

# init metrics
metric_namespace = os.environ.get("METRIC_NAMESPACE", "")
cloudwatch = boto3.client("cloudwatch") if metric_namespace else None

# other constants
search_limit = 20
max_answer_tokens = 500
role_content = "You are an apostle of the Church of Jesus Christ of Latter-day Saints"
prompt_content = (
    "Answer the question as truthfully as possible using the provided context, "
    + 'and if answer is not contained within the text below, say "Sorry, I don\'t know".'
)


# data models
class SearchResult(BaseModel):
    """Search result."""

    id: str
    title: str
    author: str
    url: str
    text: str


class SearchResponse(BaseModel):
    """Search response."""

    q: str
    session: int
    answer: str
    results: list[SearchResult]


@app.middleware("http")
async def log_exceptions_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log exceptions."""
    try:
        return await call_next(request)
    except Exception:
        body = await request.body()
        logger.error(
            traceback.format_exc(),
            extra={
                "url": request.url,
                "method": request.method,
                # "headers": request.headers,
                "body": body,
            },
        )
        return Response(status_code=500, content="Internal Server Error")


@app.get("/search")
async def search(
    q: str = Query(max_length=100),
    background_tasks: BackgroundTasks = BackgroundTasks(),  # noqa: B008
) -> SearchResponse:
    """Search."""
    # get query embedding
    start = time.perf_counter()
    embed_response = openai.Embedding.create(
        input=[q], engine=embedding_model
    )  # type: ignore
    embed_secs = time.perf_counter() - start
    embedding = embed_response["data"][0]["embedding"]

    # query index
    start = time.perf_counter()
    query_response = index.query(embedding, top_k=search_limit, include_metadata=True)
    index_secs = time.perf_counter() - start

    # get prompt
    texts = [res["metadata"]["text"] for res in query_response["matches"]]
    prompt, n_contexts = get_prompt(prompt_content, q, texts, prompt_limit)

    # get answer
    start = time.perf_counter()
    answer_response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": role_content,
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=max_answer_tokens,
        frequency_penalty=0,
        presence_penalty=0,
        stop=None,
    )  # type: ignore
    answer_secs = time.perf_counter() - start
    answer = answer_response["choices"][0]["message"]["content"].strip()

    # create response
    response = SearchResponse(
        q=q,
        session=random.getrandbits(32),
        answer=answer,
        results=[
            SearchResult(
                id=res["id"],
                title=res["metadata"]["title"],
                text=res["metadata"]["text"],
                author=res["metadata"]["author"],
                url=res["metadata"]["url"],
            )
            for res in query_response["matches"]
        ],
    )

    logger.info(
        "search",
        extra={
            "role": role_content,
            "prefix": prompt_content,
            "response": response.dict(),
        },
    )
    if cloudwatch:
        background_tasks.add_task(
            log_metrics,
            cloudwatch,
            metric_namespace,
            "search",
            embed_secs,
            index_secs,
            answer_secs,
            len(prompt),
            n_contexts,
            len(answer),
        )
    return response


class RatingScore(IntEnum):
    """Valid rating score."""

    UP = 1
    UP2 = 2
    DOWN = -1
    DOWN2 = -2


class Rating(BaseModel):
    """Rating."""

    session: int
    user: int
    result: int
    score: RatingScore


@app.post("/rate")
async def rate(rating: Rating) -> Response:
    """Rate."""
    logger.info(
        "rate",
        extra={
            "session": rating.session,
            "user": rating.user,
            "result": rating.result,
            "score": rating.score,
        },
    )
    return Response(status_code=201)


@app.get("/health")
async def health() -> Response:
    """Health check."""
    logger.info("health")
    return Response(status_code=200, content="OK")
