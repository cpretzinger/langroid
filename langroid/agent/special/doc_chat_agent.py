"""
Agent that supports asking queries about a set of documents, using
retrieval-augmented generation (RAG).

Functionality includes:
- summarizing a document, with a custom instruction; see `summarize_docs`
- asking a question about a document; see `answer_from_docs`

Note: to use the sentence-transformer embeddings, you must install
langroid with the [hf-embeddings] extra, e.g.:

pip install "langroid[hf-embeddings]"

"""

import logging
from contextlib import ExitStack
from functools import cache
from typing import Any, Dict, List, Optional, Set, Tuple, no_type_check

import nest_asyncio
import numpy as np
import pandas as pd
from rich.prompt import Prompt

from langroid.agent.batch import run_batch_tasks
from langroid.agent.chat_agent import ChatAgent, ChatAgentConfig
from langroid.agent.chat_document import ChatDocMetaData, ChatDocument
from langroid.agent.special.relevance_extractor_agent import (
    RelevanceExtractorAgent,
    RelevanceExtractorAgentConfig,
)
from langroid.agent.task import Task
from langroid.embedding_models.models import OpenAIEmbeddingsConfig
from langroid.language_models.base import StreamingIfAllowed
from langroid.language_models.openai_gpt import OpenAIChatModel, OpenAIGPTConfig
from langroid.mytypes import DocMetaData, Document, Entity
from langroid.parsing.document_parser import DocumentType
from langroid.parsing.parser import Parser, ParsingConfig, PdfParsingConfig, Splitter
from langroid.parsing.repo_loader import RepoLoader
from langroid.parsing.search import (
    find_closest_matches_with_bm25,
    find_fuzzy_matches_in_docs,
    preprocess_text,
)
from langroid.parsing.table_loader import describe_dataframe
from langroid.parsing.url_loader import URLLoader
from langroid.parsing.urls import get_list_from_user, get_urls_paths_bytes_indices
from langroid.parsing.utils import batched
from langroid.prompts.prompts_config import PromptsConfig
from langroid.prompts.templates import SUMMARY_ANSWER_PROMPT_GPT4
from langroid.utils.configuration import settings
from langroid.utils.constants import NO_ANSWER
from langroid.utils.output import show_if_debug, status
from langroid.utils.pydantic_utils import dataframe_to_documents, extract_fields
from langroid.vector_store.base import VectorStore, VectorStoreConfig
from langroid.vector_store.lancedb import LanceDBConfig


@cache
def apply_nest_asyncio() -> None:
    nest_asyncio.apply()


logger = logging.getLogger(__name__)

DEFAULT_DOC_CHAT_INSTRUCTIONS = """
Your task is to answer questions about various documents.
You will be given various passages from these documents, and asked to answer questions
about them, or summarize them into coherent answers.
"""

DEFAULT_DOC_CHAT_SYSTEM_MESSAGE = """
You are a helpful assistant, helping me understand a collection of documents.
"""

has_sentence_transformers = False
try:
    from sentence_transformers import SentenceTransformer  # noqa: F401

    has_sentence_transformers = True
except ImportError:
    pass


class DocChatAgentConfig(ChatAgentConfig):
    system_message: str = DEFAULT_DOC_CHAT_SYSTEM_MESSAGE
    user_message: str = DEFAULT_DOC_CHAT_INSTRUCTIONS
    summarize_prompt: str = SUMMARY_ANSWER_PROMPT_GPT4
    # extra fields to include in content as key=value pairs
    # (helps retrieval for table-like data)
    add_fields_to_content: List[str] = []
    filter_fields: List[str] = []  # fields usable in filter
    retrieve_only: bool = False  # only retr relevant extracts, don't gen summary answer
    extraction_granularity: int = 1  # granularity (in sentences) for relev extraction
    filter: str | None = (
        None  # filter condition for various lexical/semantic search fns
    )
    conversation_mode: bool = True  # accumulate message history?
    # In assistant mode, DocChatAgent receives questions from another Agent,
    # and those will already be in stand-alone form, so in this mode
    # there is no need to convert them to stand-alone form.
    assistant_mode: bool = False
    # Use LLM to generate hypothetical answer A to the query Q,
    # and use the embed(A) to find similar chunks in vecdb.
    # Referred to as HyDE in the paper:
    # https://arxiv.org/pdf/2212.10496.pdf
    # It is False by default; its benefits depends on the context.
    hypothetical_answer: bool = False
    n_query_rephrases: int = 0
    n_neighbor_chunks: int = 0  # how many neighbors on either side of match to retrieve
    n_fuzzy_neighbor_words: int = 100  # num neighbor words to retrieve for fuzzy match
    use_fuzzy_match: bool = True
    use_bm25_search: bool = True
    cross_encoder_reranking_model: str = (
        "cross-encoder/ms-marco-MiniLM-L-6-v2" if has_sentence_transformers else ""
    )
    rerank_diversity: bool = True  # rerank to maximize diversity?
    rerank_periphery: bool = True  # rerank to avoid Lost In the Middle effect?
    embed_batch_size: int = 500  # get embedding of at most this many at a time
    cache: bool = True  # cache results
    debug: bool = False
    stream: bool = True  # allow streaming where needed
    split: bool = True  # use chunking
    relevance_extractor_config: None | RelevanceExtractorAgentConfig = (
        RelevanceExtractorAgentConfig(
            llm=None  # use the parent's llm unless explicitly set here
        )
    )
    doc_paths: List[str | bytes] = []
    default_paths: List[str] = [
        "https://news.ycombinator.com/item?id=35629033",
        "https://www.newyorker.com/tech/annals-of-technology/chatgpt-is-a-blurry-jpeg-of-the-web",
        "https://www.wired.com/1995/04/maes/",
        "https://cthiriet.com/articles/scaling-laws",
        "https://www.jasonwei.net/blog/emergence",
        "https://www.quantamagazine.org/the-unpredictable-abilities-emerging-from-large-ai-models-20230316/",
        "https://ai.googleblog.com/2022/11/characterizing-emergent-phenomena-in.html",
    ]
    parsing: ParsingConfig = ParsingConfig(  # modify as needed
        splitter=Splitter.TOKENS,
        chunk_size=1000,  # aim for this many tokens per chunk
        overlap=100,  # overlap between chunks
        max_chunks=10_000,
        # aim to have at least this many chars per chunk when
        # truncating due to punctuation
        min_chunk_chars=200,
        discard_chunk_chars=5,  # discard chunks with fewer than this many chars
        n_similar_docs=3,
        n_neighbor_ids=0,  # num chunk IDs to store on either side of each chunk
        pdf=PdfParsingConfig(
            # NOTE: PDF parsing is extremely challenging, and each library
            # has its own strengths and weaknesses.
            # Try one that works for your use case.
            # or "unstructured", "pdfplumber", "fitz", "pypdf"
            library="pdfplumber",
        ),
    )
    from langroid.embedding_models.models import SentenceTransformerEmbeddingsConfig

    hf_embed_config = SentenceTransformerEmbeddingsConfig(
        model_type="sentence-transformer",
        model_name="BAAI/bge-large-en-v1.5",
    )

    oai_embed_config = OpenAIEmbeddingsConfig(
        model_type="openai",
        model_name="text-embedding-ada-002",
        dims=1536,
    )

    vecdb: VectorStoreConfig = LanceDBConfig(
        collection_name="doc-chat-lancedb",
        replace_collection=True,
        storage_path=".lancedb/data/",
        embedding=hf_embed_config if has_sentence_transformers else oai_embed_config,
    )
    llm: OpenAIGPTConfig = OpenAIGPTConfig(
        type="openai",
        chat_model=OpenAIChatModel.GPT4,
        completion_model=OpenAIChatModel.GPT4,
        timeout=40,
    )
    prompts: PromptsConfig = PromptsConfig(
        max_tokens=1000,
    )


class DocChatAgent(ChatAgent):
    """
    Agent for chatting with a collection of documents.
    """

    def __init__(
        self,
        config: DocChatAgentConfig,
    ):
        super().__init__(config)
        self.config: DocChatAgentConfig = config
        self.original_docs: List[Document] = []
        self.original_docs_length = 0
        self.from_dataframe = False
        self.df_description = ""
        self.chunked_docs: List[Document] = []
        self.chunked_docs_clean: List[Document] = []
        self.response: None | Document = None
        if len(config.doc_paths) > 0:
            self.ingest()

    def clear(self) -> None:
        """Clear the document collection and the specific collection in vecdb"""
        if self.vecdb is None:
            raise ValueError("VecDB not set")
        self.original_docs = []
        self.original_docs_length = 0
        self.chunked_docs = []
        self.chunked_docs_clean = []
        collection_name = self.vecdb.config.collection_name
        if collection_name is None:
            return
        try:
            # Note we may have used a vecdb with a config.collection_name
            # different from the agent's config.vecdb.collection_name!!
            self.vecdb.delete_collection(collection_name)
            self.vecdb = VectorStore.create(self.vecdb.config)
        except Exception as e:
            logger.warning(
                f"""
                Error while deleting collection {collection_name}:
                {e}
                """
            )

    def ingest(self) -> None:
        """
        Chunk + embed + store docs specified by self.config.doc_paths

        Returns:
            dict with keys:
                n_splits: number of splits
                urls: list of urls
                paths: list of file paths
        """
        if len(self.config.doc_paths) == 0:
            # we must be using a previously defined collection
            # But let's get all the chunked docs so we can
            # do keyword and other non-vector searches
            if self.vecdb is None:
                raise ValueError("VecDB not set")
            self.setup_documents(filter=self.config.filter)
            return
        self.ingest_doc_paths(self.config.doc_paths)  # type: ignore

    def ingest_doc_paths(
        self,
        paths: str | bytes | List[str | bytes],
        metadata: (
            List[Dict[str, Any]] | Dict[str, Any] | DocMetaData | List[DocMetaData]
        ) = [],
        doc_type: str | DocumentType | None = None,
    ) -> List[Document]:
        """Split, ingest docs from specified paths,
        do not add these to config.doc_paths.

        Args:
            paths: document paths, urls or byte-content of docs.
                The bytes option is intended to support cases where a document
                has already been read in as bytes (e.g. from an API or a database),
                and we want to avoid having to write it to a temporary file
                just to read it back in.
            metadata: List of metadata dicts, one for each path.
                If a single dict is passed in, it is used for all paths.
            doc_type: DocumentType to use for parsing, if known.
                MUST apply to all docs if specified.
                This is especially useful when the `paths` are of bytes type,
                to help with document type detection.
        Returns:
            List of Document objects
        """
        if isinstance(paths, str) or isinstance(paths, bytes):
            paths = [paths]
        all_paths = paths
        paths_meta: Dict[int, Any] = {}
        urls_meta: Dict[int, Any] = {}
        idxs = range(len(all_paths))
        url_idxs, path_idxs, bytes_idxs = get_urls_paths_bytes_indices(all_paths)
        urls = [all_paths[i] for i in url_idxs]
        paths = [all_paths[i] for i in path_idxs]
        bytes_list = [all_paths[i] for i in bytes_idxs]
        path_idxs.extend(bytes_idxs)
        paths.extend(bytes_list)
        if (isinstance(metadata, list) and len(metadata) > 0) or not isinstance(
            metadata, list
        ):
            if isinstance(metadata, list):
                idx2meta = {
                    p: (
                        m
                        if isinstance(m, dict)
                        else (isinstance(m, DocMetaData) and m.dict())
                    )  # appease mypy
                    for p, m in zip(idxs, metadata)
                }
            elif isinstance(metadata, dict):
                idx2meta = {p: metadata for p in idxs}
            else:
                idx2meta = {p: metadata.dict() for p in idxs}
            urls_meta = {u: idx2meta[u] for u in url_idxs}
            paths_meta = {p: idx2meta[p] for p in path_idxs}
        docs: List[Document] = []
        parser = Parser(self.config.parsing)
        if len(urls) > 0:
            for ui in url_idxs:
                meta = urls_meta.get(ui, {})
                loader = URLLoader(urls=[all_paths[ui]], parser=parser)  # type: ignore
                url_docs = loader.load()
                # update metadata of each doc with meta
                for d in url_docs:
                    d.metadata = d.metadata.copy(update=meta)
                docs.extend(url_docs)
        if len(paths) > 0:  # paths OR bytes are handled similarly
            for pi in path_idxs:
                meta = paths_meta.get(pi, {})
                p = all_paths[pi]
                path_docs = RepoLoader.get_documents(
                    p,
                    parser=parser,
                    doc_type=doc_type,
                )
                # update metadata of each doc with meta
                for d in path_docs:
                    d.metadata = d.metadata.copy(update=meta)
                docs.extend(path_docs)
        n_docs = len(docs)
        n_splits = self.ingest_docs(docs, split=self.config.split)
        if n_docs == 0:
            return []
        n_urls = len(urls)
        n_paths = len(paths)
        print(
            f"""
        [green]I have processed the following {n_urls} URLs
        and {n_paths} docs into {n_splits} parts:
        """.strip()
        )
        path_reps = [p if isinstance(p, str) else "bytes" for p in paths]
        print("\n".join([u for u in urls if isinstance(u, str)]))  # appease mypy
        print("\n".join(path_reps))
        return docs

    def ingest_docs(
        self,
        docs: List[Document],
        split: bool = True,
        metadata: (
            List[Dict[str, Any]] | Dict[str, Any] | DocMetaData | List[DocMetaData]
        ) = [],
    ) -> int:
        """
        Chunk docs into pieces, map each chunk to vec-embedding, store in vec-db

        Args:
            docs: List of Document objects
            split: Whether to split docs into chunks. Default is True.
                If False, docs are treated as "chunks" and are not split.
            metadata: List of metadata dicts, one for each doc, to augment
                whatever metadata is already in the doc.
                [ASSUME no conflicting keys between the two metadata dicts.]
                If a single dict is passed in, it is used for all docs.
        """
        if isinstance(metadata, list) and len(metadata) > 0:
            for d, m in zip(docs, metadata):
                d.metadata = d.metadata.copy(
                    update=m if isinstance(m, dict) else m.dict()  # type: ignore
                )
        elif isinstance(metadata, dict):
            for d in docs:
                d.metadata = d.metadata.copy(update=metadata)
        elif isinstance(metadata, DocMetaData):
            for d in docs:
                d.metadata = d.metadata.copy(update=metadata.dict())

        self.original_docs.extend(docs)
        if self.parser is None:
            raise ValueError("Parser not set")
        for d in docs:
            if d.metadata.id in [None, ""]:
                d.metadata.id = d._unique_hash_id()
        if split:
            docs = self.parser.split(docs)
        else:
            self.parser.add_window_ids(docs)
        if self.vecdb is None:
            raise ValueError("VecDB not set")

        # If any additional fields need to be added to content,
        # add them as key=value pairs for all docs, before batching.
        # This helps retrieval for table-like data.
        # Note we need to do this at stage so that the embeddings
        # are computed on the full content with these additional fields.
        if len(self.config.add_fields_to_content) > 0:
            fields = [
                f for f in extract_fields(docs[0], self.config.add_fields_to_content)
            ]
            if len(fields) > 0:
                for d in docs:
                    key_vals = extract_fields(d, fields)
                    d.content = (
                        ",".join(f"{k}={v}" for k, v in key_vals.items())
                        + ",content="
                        + d.content
                    )
        docs = docs[: self.config.parsing.max_chunks]
        # add embeddings in batches, to stay under limit of embeddings API
        batches = list(batched(docs, self.config.embed_batch_size))
        for batch in batches:
            self.vecdb.add_documents(batch)
        self.original_docs_length = self.doc_length(docs)
        self.setup_documents(docs, filter=self.config.filter)
        return len(docs)

    @staticmethod
    def document_compatible_dataframe(
        df: pd.DataFrame,
        content: str = "content",
        metadata: List[str] = [],
    ) -> Tuple[pd.DataFrame, List[str]]:
        """
        Convert dataframe so it is compatible with Document class:
        - has "content" column
        - has an "id" column to be used as Document.metadata.id

        Args:
            df: dataframe to convert
            content: name of content column
            metadata: list of metadata column names

        Returns:
            Tuple[pd.DataFrame, List[str]]: dataframe, metadata
                - dataframe: dataframe with "content" column and "id" column
                - metadata: list of metadata column names, including "id"
        """
        if content not in df.columns:
            raise ValueError(
                f"""
                Content column {content} not in dataframe,
                so we cannot ingest into the DocChatAgent.
                Please specify the `content` parameter as a suitable
                text-based column in the dataframe.
                """
            )
        if content != "content":
            # rename content column to "content", leave existing column intact
            df = df.rename(columns={content: "content"}, inplace=False)

        actual_metadata = metadata.copy()
        if "id" not in df.columns:
            docs = dataframe_to_documents(df, content="content", metadata=metadata)
            ids = [str(d.id()) for d in docs]
            df["id"] = ids

        if "id" not in actual_metadata:
            actual_metadata += ["id"]

        return df, actual_metadata

    def ingest_dataframe(
        self,
        df: pd.DataFrame,
        content: str = "content",
        metadata: List[str] = [],
    ) -> int:
        """
        Ingest a dataframe into vecdb.
        """
        self.from_dataframe = True
        self.df_description = describe_dataframe(
            df, filter_fields=self.config.filter_fields, n_vals=5
        )
        df, metadata = DocChatAgent.document_compatible_dataframe(df, content, metadata)
        docs = dataframe_to_documents(df, content="content", metadata=metadata)
        # When ingesting a dataframe we will no longer do any chunking,
        # so we mark each doc as a chunk.
        # TODO - revisit this since we may still want to chunk large text columns
        for d in docs:
            d.metadata.is_chunk = True
        return self.ingest_docs(docs)

    def set_filter(self, filter: str) -> None:
        self.config.filter = filter
        self.setup_documents(filter=filter)

    def setup_documents(
        self,
        docs: List[Document] = [],
        filter: str | None = None,
    ) -> None:
        """
        Setup `self.chunked_docs` and `self.chunked_docs_clean`
        based on possible filter.
        These will be used in various non-vector-based search functions,
        e.g. self.get_similar_chunks_bm25(), self.get_fuzzy_matches(), etc.

        Args:
            docs: List of Document objects. This is empty when we are calling this
                method after initial doc ingestion.
            filter: Filter condition for various lexical/semantic search fns.
        """
        if filter is None and len(docs) > 0:
            # no filter, so just use the docs passed in
            self.chunked_docs.extend(docs)
        else:
            if self.vecdb is None:
                raise ValueError("VecDB not set")
            self.chunked_docs = self.vecdb.get_all_documents(where=filter or "")

        self.chunked_docs_clean = [
            Document(content=preprocess_text(d.content), metadata=d.metadata)
            for d in self.chunked_docs
        ]

    def get_field_values(self, fields: list[str]) -> Dict[str, str]:
        """Get string-listing of possible values of each filterable field,
        e.g.
        {
            "genre": "crime, drama, mystery, ... (10 more)",
            "certificate": "R, PG-13, PG, R",
        }
        """
        field_values: Dict[str, Set[str]] = {}
        # make empty set for each field
        for f in fields:
            field_values[f] = set()
        if self.vecdb is None:
            raise ValueError("VecDB not set")
        # get all documents and accumulate possible values of each field until 10
        docs = self.vecdb.get_all_documents()  # only works for vecdbs that support this
        for d in docs:
            # extract fields from d
            doc_field_vals = extract_fields(d, fields)
            for field, val in doc_field_vals.items():
                field_values[field].add(val)
        # For each field make a string showing list of possible values,
        # truncate to 20 values, and if there are more, indicate how many
        # more there are, e.g. Genre: crime, drama, mystery, ... (20 more)
        field_values_list = {}
        for f in fields:
            vals = list(field_values[f])
            n = len(vals)
            remaining = n - 20
            vals = vals[:20]
            if n > 20:
                vals.append(f"(...{remaining} more)")
            # make a string of the values, ensure they are strings
            field_values_list[f] = ", ".join(str(v) for v in vals)
        return field_values_list

    def doc_length(self, docs: List[Document]) -> int:
        """
        Calc token-length of a list of docs
        Args:
            docs: list of Document objects
        Returns:
            int: number of tokens
        """
        if self.parser is None:
            raise ValueError("Parser not set")
        return self.parser.num_tokens(self.doc_string(docs))

    def user_docs_ingest_dialog(self) -> None:
        """
        Ask user to select doc-collection, enter filenames/urls, and ingest into vecdb.
        """
        if self.vecdb is None:
            raise ValueError("VecDB not set")
        n_deletes = self.vecdb.clear_empty_collections()
        collections = self.vecdb.list_collections()
        collection_name = "NEW"
        is_new_collection = False
        replace_collection = False
        if len(collections) > 0:
            n = len(collections)
            delete_str = (
                f"(deleted {n_deletes} empty collections)" if n_deletes > 0 else ""
            )
            print(f"Found {n} collections: {delete_str}")
            for i, option in enumerate(collections, start=1):
                print(f"{i}. {option}")
            while True:
                choice = Prompt.ask(
                    f"Enter 1-{n} to select a collection, "
                    "or hit ENTER to create a NEW collection, "
                    "or -1 to DELETE ALL COLLECTIONS",
                    default="0",
                )
                try:
                    if -1 <= int(choice) <= n:
                        break
                except Exception:
                    pass

            if choice == "-1":
                confirm = Prompt.ask(
                    "Are you sure you want to delete all collections?",
                    choices=["y", "n"],
                    default="n",
                )
                if confirm == "y":
                    self.vecdb.clear_all_collections(really=True)
                    collection_name = "NEW"

            if int(choice) > 0:
                collection_name = collections[int(choice) - 1]
                print(f"Using collection {collection_name}")
                choice = Prompt.ask(
                    "Would you like to replace this collection?",
                    choices=["y", "n"],
                    default="n",
                )
                replace_collection = choice == "y"

        if collection_name == "NEW":
            is_new_collection = True
            collection_name = Prompt.ask(
                "What would you like to name the NEW collection?",
                default="doc-chat",
            )

        self.vecdb.set_collection(collection_name, replace=replace_collection)

        default_urls_str = (
            " (or leave empty for default URLs)" if is_new_collection else ""
        )
        print(f"[blue]Enter some URLs or file/dir paths below {default_urls_str}")
        inputs = get_list_from_user()
        if len(inputs) == 0:
            if is_new_collection:
                inputs = self.config.default_paths
        self.config.doc_paths = inputs  # type: ignore
        self.ingest()

    def llm_response(
        self,
        query: None | str | ChatDocument = None,
    ) -> Optional[ChatDocument]:
        if not self.llm_can_respond(query):
            return None
        query_str: str | None
        if isinstance(query, ChatDocument):
            query_str = query.content
        else:
            query_str = query
        if query_str is None or query_str.startswith("!"):
            # direct query to LLM
            query_str = query_str[1:] if query_str is not None else None
            if self.llm is None:
                raise ValueError("LLM not set")
            with StreamingIfAllowed(self.llm, self.llm.get_stream()):
                response = super().llm_response(query_str)
            if query_str is not None:
                self.update_dialog(
                    query_str, "" if response is None else response.content
                )
            return response
        if query_str == "":
            return None
        elif query_str == "?" and self.response is not None:
            return self.justify_response()
        elif (query_str.startswith(("summar", "?")) and self.response is None) or (
            query_str == "??"
        ):
            return self.summarize_docs()
        else:
            self.callbacks.show_start_response(entity="llm")
            response = self.answer_from_docs(query_str)
            return ChatDocument(
                content=response.content,
                metadata=ChatDocMetaData(
                    source=response.metadata.source,
                    sender=Entity.LLM,
                ),
            )

    async def llm_response_async(
        self,
        query: None | str | ChatDocument = None,
    ) -> Optional[ChatDocument]:
        apply_nest_asyncio()
        if not self.llm_can_respond(query):
            return None
        query_str: str | None
        if isinstance(query, ChatDocument):
            query_str = query.content
        else:
            query_str = query
        if query_str is None or query_str.startswith("!"):
            # direct query to LLM
            query_str = query_str[1:] if query_str is not None else None
            if self.llm is None:
                raise ValueError("LLM not set")
            with StreamingIfAllowed(self.llm, self.llm.get_stream()):
                response = await super().llm_response_async(query_str)
            if query_str is not None:
                self.update_dialog(
                    query_str, "" if response is None else response.content
                )
            return response
        if query_str == "":
            return None
        elif query_str == "?" and self.response is not None:
            return self.justify_response()
        elif (query_str.startswith(("summar", "?")) and self.response is None) or (
            query_str == "??"
        ):
            return self.summarize_docs()
        else:
            self.callbacks.show_start_response(entity="llm")
            response = self.answer_from_docs(query_str)
            return ChatDocument(
                content=response.content,
                metadata=ChatDocMetaData(
                    source=response.metadata.source,
                    sender=Entity.LLM,
                ),
            )

    @staticmethod
    def doc_string(docs: List[Document]) -> str:
        """
        Generate a string representation of a list of docs.
        Args:
            docs: list of Document objects
        Returns:
            str: string representation
        """
        contents = [f"Extract: {d.content}" for d in docs]
        sources = [d.metadata.source for d in docs]
        sources = [f"Source: {s}" if s is not None else "" for s in sources]
        return "\n".join(
            [
                f"""
                {content}
                {source}
                """
                for (content, source) in zip(contents, sources)
            ]
        )

    def get_summary_answer(
        self, question: str, passages: List[Document]
    ) -> ChatDocument:
        """
        Given a question and a list of (possibly) doc snippets,
        generate an answer if possible
        Args:
            question: question to answer
            passages: list of `Document` objects each containing a possibly relevant
                snippet, and metadata
        Returns:
            a `Document` object containing the answer,
            and metadata containing source citations

        """

        passages_str = self.doc_string(passages)
        # Substitute Q and P into the templatized prompt

        final_prompt = self.config.summarize_prompt.format(
            question=f"Question:{question}", extracts=passages_str
        )
        show_if_debug(final_prompt, "SUMMARIZE_PROMPT= ")

        # Generate the final verbatim extract based on the final prompt.
        # Note this will send entire message history, plus this final_prompt
        # to the LLM, and self.message_history will be updated to include
        # 2 new LLMMessage objects:
        # one for `final_prompt`, and one for the LLM response

        if self.config.conversation_mode:
            # respond with temporary context
            answer_doc = super()._llm_response_temp_context(question, final_prompt)
        else:
            answer_doc = super().llm_response_forget(final_prompt)

        final_answer = answer_doc.content.strip()
        show_if_debug(final_answer, "SUMMARIZE_RESPONSE= ")

        if final_answer.startswith("SOURCE"):
            # sometimes SOURCE may be shown first,
            # in this case just use final_answer as-is for both content and source
            content = final_answer
            sources = final_answer
        else:
            parts = final_answer.split("SOURCE:", maxsplit=1)
            if len(parts) > 1:
                content = parts[0].strip()
                sources = parts[1].strip()
            else:
                content = final_answer
                sources = ""
        return ChatDocument(
            content=content,
            metadata=ChatDocMetaData(
                source="SOURCE: " + sources,
                sender=Entity.LLM,
                cached=getattr(answer_doc.metadata, "cached", False),
            ),
        )

    def llm_hypothetical_answer(self, query: str) -> str:
        if self.llm is None:
            raise ValueError("LLM not set")
        with status("[cyan]LLM generating hypothetical answer..."):
            with StreamingIfAllowed(self.llm, False):
                # TODO: provide an easy way to
                # Adjust this prompt depending on context.
                answer = self.llm_response_forget(
                    f"""
                    Give an ideal answer to the following query,
                    in up to 3 sentences. Do not explain yourself,
                    and do not apologize, just show
                    a good possible answer, even if you do not have any information.
                    Preface your answer with "HYPOTHETICAL ANSWER: "

                    QUERY: {query}
                    """
                ).content
        return answer

    def llm_rephrase_query(self, query: str) -> List[str]:
        if self.llm is None:
            raise ValueError("LLM not set")
        with status("[cyan]LLM generating rephrases of query..."):
            with StreamingIfAllowed(self.llm, False):
                rephrases = self.llm_response_forget(
                    f"""
                        Rephrase the following query in {self.config.n_query_rephrases}
                        different equivalent ways, separate them with 2 newlines.
                        QUERY: {query}
                        """
                ).content.split("\n\n")
        return rephrases

    def get_similar_chunks_bm25(
        self, query: str, multiple: int
    ) -> List[Tuple[Document, float]]:
        # find similar docs using bm25 similarity:
        # these may sometimes be more likely to contain a relevant verbatim extract
        with status("[cyan]Searching for similar chunks using bm25..."):
            if self.chunked_docs is None or len(self.chunked_docs) == 0:
                logger.warning("No chunked docs; cannot use bm25-similarity")
                return []
            if self.chunked_docs_clean is None or len(self.chunked_docs_clean) == 0:
                logger.warning("No cleaned chunked docs; cannot use bm25-similarity")
                return []
            docs_scores = find_closest_matches_with_bm25(
                self.chunked_docs,
                self.chunked_docs_clean,  # already pre-processed!
                query,
                k=self.config.parsing.n_similar_docs * multiple,
            )
        return docs_scores

    def get_fuzzy_matches(self, query: str, multiple: int) -> List[Document]:
        # find similar docs using fuzzy matching:
        # these may sometimes be more likely to contain a relevant verbatim extract
        with status("[cyan]Finding fuzzy matches in chunks..."):
            if self.chunked_docs is None:
                logger.warning("No chunked docs; cannot use fuzzy matching")
                return []
            if self.chunked_docs_clean is None:
                logger.warning("No cleaned chunked docs; cannot use fuzzy-search")
                return []
            fuzzy_match_docs = find_fuzzy_matches_in_docs(
                query,
                self.chunked_docs,
                self.chunked_docs_clean,
                k=self.config.parsing.n_similar_docs * multiple,
                words_before=self.config.n_fuzzy_neighbor_words,
                words_after=self.config.n_fuzzy_neighbor_words,
            )
        return fuzzy_match_docs

    def rerank_with_cross_encoder(
        self, query: str, passages: List[Document]
    ) -> List[Document]:
        with status("[cyan]Re-ranking retrieved chunks using cross-encoder..."):
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                raise ImportError(
                    """
                    To use cross-encoder re-ranking, you must install
                    langroid with the [hf-embeddings] extra, e.g.:
                    pip install "langroid[hf-embeddings]"
                    """
                )

            model = CrossEncoder(self.config.cross_encoder_reranking_model)
            scores = model.predict([(query, p.content) for p in passages])
            # Convert to [0,1] so we might could use a cutoff later.
            scores = 1.0 / (1 + np.exp(-np.array(scores)))
            # get top k scoring passages
            sorted_pairs = sorted(
                zip(scores, passages),
                key=lambda x: x[0],
                reverse=True,
            )
            passages = [
                d for _, d in sorted_pairs[: self.config.parsing.n_similar_docs]
            ]
        return passages

    def rerank_with_diversity(self, passages: List[Document]) -> List[Document]:
        """
        Rerank a list of items in such a way that each successive item is least similar
        (on average) to the earlier items.

        Args:
        query (str): The query for which the passages are relevant.
        passages (List[Document]): A list of Documents to be reranked.

        Returns:
        List[Documents]: A reranked list of Documents.
        """

        if self.vecdb is None:
            logger.warning("No vecdb; cannot use rerank_with_diversity")
            return passages
        emb_model = self.vecdb.embedding_model
        emb_fn = emb_model.embedding_fn()
        embs = emb_fn([p.content for p in passages])
        embs_arr = [np.array(e) for e in embs]
        indices = list(range(len(passages)))

        # Helper function to compute average similarity to
        # items in the current result list.
        def avg_similarity_to_result(i: int, result: List[int]) -> float:
            return sum(  # type: ignore
                (embs_arr[i] @ embs_arr[j])
                / (np.linalg.norm(embs_arr[i]) * np.linalg.norm(embs_arr[j]))
                for j in result
            ) / len(result)

        # copy passages to items
        result = [indices.pop(0)]  # Start with the first item.

        while indices:
            # Find the item that has the least average similarity
            # to items in the result list.
            least_similar_item = min(
                indices, key=lambda i: avg_similarity_to_result(i, result)
            )
            result.append(least_similar_item)
            indices.remove(least_similar_item)

        # return passages in order of result list
        return [passages[i] for i in result]

    def rerank_to_periphery(self, passages: List[Document]) -> List[Document]:
        """
        Rerank to avoid Lost In the Middle (LIM) problem,
        where LLMs pay more attention to items at the ends of a list,
        rather than the middle. So we re-rank to make the best passages
        appear at the periphery of the list.
        https://arxiv.org/abs/2307.03172

        Example reranking:
        1 2 3 4 5 6 7 8 9 ==> 1 3 5 7 9 8 6 4 2

        Args:
            passages (List[Document]): A list of Documents to be reranked.

        Returns:
            List[Documents]: A reranked list of Documents.

        """
        # Splitting items into odds and evens based on index, not value
        odds = passages[::2]
        evens = passages[1::2][::-1]

        # Merging them back together
        return odds + evens

    def add_context_window(
        self,
        docs_scores: List[Tuple[Document, float]],
    ) -> List[Tuple[Document, float]]:
        """
        In each doc's metadata, there may be a window_ids field indicating
        the ids of the chunks around the current chunk. We use these stored
        window_ids to retrieve the desired number
        (self.config.n_neighbor_chunks) of neighbors
        on either side of the current chunk.

        Args:
            docs_scores (List[Tuple[Document, float]]): List of pairs of documents
                to add context windows to together with their match scores.

        Returns:
            List[Tuple[Document, float]]: List of (Document, score) tuples.
        """
        if self.vecdb is None or self.config.n_neighbor_chunks == 0:
            return docs_scores
        if len(docs_scores) == 0:
            return []
        if set(docs_scores[0][0].__fields__) != {"content", "metadata"}:
            # Do not add context window when there are other fields besides just
            # content and metadata, since we do not know how to set those other fields
            # for newly created docs with combined content.
            return docs_scores
        return self.vecdb.add_context_window(docs_scores, self.config.n_neighbor_chunks)

    def get_semantic_search_results(
        self,
        query: str,
        k: int = 10,
    ) -> List[Tuple[Document, float]]:
        """
        Get semantic search results from vecdb.
        Args:
            query (str): query to search for
            k (int): number of results to return
        Returns:
            List[Tuple[Document, float]]: List of (Document, score) tuples.
        """
        if self.vecdb is None:
            raise ValueError("VecDB not set")
        # Note: for dynamic filtering based on a query, users can
        # use the `temp_update` context-manager to pass in a `filter` to self.config,
        # e.g.:
        # with temp_update(self.config, {"filter": "metadata.source=='source1'"}):
        #     docs_scores = self.get_semantic_search_results(query, k=k)
        # This avoids having pass the `filter` argument to every function call
        # upstream of this one.
        # The `temp_update` context manager is defined in
        # `langroid/utils/pydantic_utils.py`
        return self.vecdb.similar_texts_with_scores(
            query,
            k=k,
            where=self.config.filter,
        )

    def get_relevant_chunks(
        self, query: str, query_proxies: List[str] = []
    ) -> List[Document]:
        """
        The retrieval stage in RAG: get doc-chunks that are most "relevant"
        to the query (and possibly any proxy queries), from the document-store,
        which currently is the vector store,
        but in theory could be any document store, or even web-search.
        This stage does NOT involve an LLM, and the retrieved chunks
        could either be pre-chunked text (from the initial pre-processing stage
        where chunks were stored in the vector store), or they could be
        dynamically retrieved based on a window around a lexical match.

        These are the steps (some optional based on config):
        - semantic search based on vector-embedding distance, from vecdb
        - lexical search using bm25-ranking (keyword similarity)
        - fuzzy matching (keyword similarity)
        - re-ranking of doc-chunks by relevance to query, using cross-encoder,
           and pick top k

        Args:
            query: original query (assumed to be in stand-alone form)
            query_proxies: possible rephrases, or hypothetical answer to query
                    (e.g. for HyDE-type retrieval)

        Returns:

        """
        # if we are using cross-encoder reranking, we can retrieve more docs
        # during retrieval, and leave it to the cross-encoder re-ranking
        # to whittle down to self.config.parsing.n_similar_docs
        retrieval_multiple = 1 if self.config.cross_encoder_reranking_model == "" else 3

        if self.vecdb is None:
            raise ValueError("VecDB not set")

        with status("[cyan]Searching VecDB for relevant doc passages..."):
            docs_and_scores: List[Tuple[Document, float]] = []
            for q in [query] + query_proxies:
                docs_and_scores += self.get_semantic_search_results(
                    q,
                    k=self.config.parsing.n_similar_docs * retrieval_multiple,
                )
        # keep only docs with unique d.id()
        id2doc_score = {d.id(): (d, s) for d, s in docs_and_scores}
        docs_and_scores = list(id2doc_score.values())
        passages = [d for (d, _) in docs_and_scores]
        # passages = [
        #     Document(content=d.content, metadata=d.metadata)
        #     for (d, _) in docs_and_scores
        # ]

        if self.config.use_bm25_search:
            docs_scores = self.get_similar_chunks_bm25(query, retrieval_multiple)
            passages += [d for (d, _) in docs_scores]

        if self.config.use_fuzzy_match:
            fuzzy_match_docs = self.get_fuzzy_matches(query, retrieval_multiple)
            passages += fuzzy_match_docs

        # keep unique passages
        id2passage = {p.id(): p for p in passages}
        passages = list(id2passage.values())

        if len(passages) == 0:
            return []

        passages_scores = [(p, 0.0) for p in passages]
        passages_scores = self.add_context_window(passages_scores)
        passages = [p for p, _ in passages_scores]
        # now passages can potentially have a lot of doc chunks,
        # so we re-rank them using a cross-encoder scoring model,
        # and pick top k where k = config.parsing.n_similar_docs
        # https://www.sbert.net/examples/applications/retrieve_rerank
        if self.config.cross_encoder_reranking_model != "":
            passages = self.rerank_with_cross_encoder(query, passages)

        if self.config.rerank_diversity:
            # reorder to increase diversity among top docs
            passages = self.rerank_with_diversity(passages)

        if self.config.rerank_periphery:
            # reorder so most important docs are at periphery
            # (see Lost In the Middle issue).
            passages = self.rerank_to_periphery(passages)

        return passages

    @no_type_check
    def get_relevant_extracts(self, query: str) -> Tuple[str, List[Document]]:
        """
        Get list of (verbatim) extracts from doc-chunks relevant to answering a query.

        These are the stages (some optional based on config):
        - use LLM to convert query to stand-alone query
        - optionally use LLM to rephrase query to use below
        - optionally use LLM to generate hypothetical answer (HyDE) to use below.
        - get_relevant_chunks(): get doc-chunks relevant to query and proxies
        - use LLM to get relevant extracts from doc-chunks

        Args:
            query (str): query to search for

        Returns:
            query (str): stand-alone version of input query
            List[Document]: list of relevant extracts

        """
        if len(self.dialog) > 0 and not self.config.assistant_mode:
            # Regardless of whether we are in conversation mode or not,
            # for relevant doc/chunk extraction, we must convert the query
            # to a standalone query to get more relevant results.
            with status("[cyan]Converting to stand-alone query...[/cyan]"):
                with StreamingIfAllowed(self.llm, False):
                    query = self.llm.followup_to_standalone(self.dialog, query)
            print(f"[orange2]New query: {query}")

        proxies = []
        if self.config.hypothetical_answer:
            answer = self.llm_hypothetical_answer(query)
            proxies = [answer]

        if self.config.n_query_rephrases > 0:
            rephrases = self.llm_rephrase_query(query)
            proxies += rephrases

        passages = self.get_relevant_chunks(query, proxies)  # no LLM involved

        if len(passages) == 0:
            return query, []

        with status("[cyan]LLM Extracting verbatim passages..."):
            with StreamingIfAllowed(self.llm, False):
                # these are async calls, one per passage; turn off streaming
                extracts = self.get_verbatim_extracts(query, passages)
                extracts = [e for e in extracts if e.content != NO_ANSWER]

        return query, extracts

    def get_verbatim_extracts(
        self,
        query: str,
        passages: List[Document],
    ) -> List[Document]:
        """
        Run RelevanceExtractorAgent in async/concurrent mode on passages,
        to extract portions relevant to answering query, from each passage.
        Args:
            query (str): query to answer
            passages (List[Documents]): list of passages to extract from

        Returns:
            List[Document]: list of Documents containing extracts and metadata.
        """
        agent_cfg = self.config.relevance_extractor_config
        if agent_cfg is None:
            # no relevance extraction: simply return passages
            return passages
        if agent_cfg.llm is None:
            # Use main DocChatAgent's LLM if not provided explicitly:
            # this reduces setup burden on the user
            agent_cfg.llm = self.config.llm
        agent_cfg.query = query
        agent_cfg.segment_length = self.config.extraction_granularity
        agent_cfg.llm.stream = False  # disable streaming for concurrent calls

        agent = RelevanceExtractorAgent(agent_cfg)
        task = Task(
            agent,
            name="Relevance-Extractor",
            interactive=False,
        )

        extracts = run_batch_tasks(
            task,
            passages,
            input_map=lambda msg: msg.content,
            output_map=lambda ans: ans.content if ans is not None else NO_ANSWER,
        )

        # Caution: Retain ALL other fields in the Documents (which could be
        # other than just `content` and `metadata`), while simply replacing
        # `content` with the extracted portions
        passage_extracts = []
        for p, e in zip(passages, extracts):
            if e == NO_ANSWER or len(e) == 0:
                continue
            p_copy = p.copy()
            p_copy.content = e
            passage_extracts.append(p_copy)

        return passage_extracts

    def answer_from_docs(self, query: str) -> ChatDocument:
        """
        Answer query based on relevant docs from the VecDB

        Args:
            query (str): query to answer

        Returns:
            Document: answer
        """
        response = ChatDocument(
            content=NO_ANSWER,
            metadata=ChatDocMetaData(
                source="None",
                sender=Entity.LLM,
            ),
        )
        # query may be updated to a stand-alone version
        query, extracts = self.get_relevant_extracts(query)
        if len(extracts) == 0:
            return response
        if self.llm is None:
            raise ValueError("LLM not set")
        if self.config.retrieve_only:
            # only return extracts, skip LLM-based summary answer
            meta = dict(
                sender=Entity.LLM,
            )
            # copy metadata from first doc, unclear what to do here.
            meta.update(extracts[0].metadata)
            return ChatDocument(
                content="\n\n".join([e.content for e in extracts]),
                metadata=ChatDocMetaData(**meta),
            )
        with ExitStack() as stack:
            # conditionally use Streaming or rich console context
            cm = (
                StreamingIfAllowed(self.llm)
                if settings.stream
                else (status("LLM Generating final answer..."))
            )
            stack.enter_context(cm)  # type: ignore
            response = self.get_summary_answer(query, extracts)

        self.update_dialog(query, response.content)
        self.response = response  # save last response
        return response

    def summarize_docs(
        self,
        instruction: str = "Give a concise summary of the following text:",
    ) -> None | ChatDocument:
        """Summarize all docs"""
        if self.llm is None:
            raise ValueError("LLM not set")
        if len(self.original_docs) == 0:
            logger.warning(
                """
                No docs to summarize! Perhaps you are re-using a previously
                defined collection?
                In that case, we don't have access to the original docs.
                To create a summary, use a new collection, and specify a list of docs.
                """
            )
            return None
        full_text = "\n\n".join([d.content for d in self.original_docs])
        if self.parser is None:
            raise ValueError("No parser defined")
        tot_tokens = self.parser.num_tokens(full_text)
        MAX_INPUT_TOKENS = (
            self.llm.completion_context_length()
            - self.config.llm.max_output_tokens
            - 100
        )
        if tot_tokens > MAX_INPUT_TOKENS:
            # truncate
            full_text = self.parser.tokenizer.decode(
                self.parser.tokenizer.encode(full_text)[:MAX_INPUT_TOKENS]
            )
            logger.warning(
                f"Summarizing after truncating text to {MAX_INPUT_TOKENS} tokens"
            )
        prompt = f"""
        {instruction}

        FULL TEXT:
        {full_text}
        """.strip()
        with StreamingIfAllowed(self.llm):
            summary = ChatAgent.llm_response(self, prompt)
            return summary

    def justify_response(self) -> ChatDocument | None:
        """Show evidence for last response"""
        if self.response is None:
            print("[magenta]No response yet")
            return None
        source = self.response.metadata.source
        if len(source) > 0:
            print("[magenta]" + source)
        else:
            print("[magenta]No source found")
        return None
