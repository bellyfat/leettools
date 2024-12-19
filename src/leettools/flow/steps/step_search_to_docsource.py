from datetime import datetime
from typing import ClassVar, List, Optional, Type

from leettools.common import exceptions
from leettools.common.logging import logger
from leettools.common.utils import config_utils
from leettools.core.consts import flow_option
from leettools.core.consts.docsource_status import DocSourceStatus
from leettools.core.consts.docsource_type import DocSourceType
from leettools.core.consts.retriever_type import RetrieverType
from leettools.core.schemas.docsource import DocSource, DocSourceCreate, IngestConfig
from leettools.core.schemas.document import Document
from leettools.eds.scheduler.scheduler_manager import run_scheduler
from leettools.flow import flow_option_items
from leettools.flow.exec_info import ExecInfo
from leettools.flow.flow_component import FlowComponent
from leettools.flow.flow_option_items import FlowOptionItem
from leettools.flow.step import AbstractStep
from leettools.flow.utils import pipeline_utils
from leettools.web import search_utils
from leettools.web.web_searcher import WebSearcher


class StepSearchToDocsource(AbstractStep):

    COMPONENT_NAME: ClassVar[str] = "search_to_docsource"

    @classmethod
    def short_description(cls) -> str:
        return "Search the web for related documents and save them to the docsource."

    @classmethod
    def full_description(cls) -> str:
        return """Create a document source with web search. 

For knowledge base that has auto_schedule set to True, if a scheduler is running, the 
document source will be scheduled for processing, otherwise, the scheduler will bestarted
to process the document source. The actual web seacher will be started by the scheduler
using the config in the exec_info. This function will wait for the document source to 
finish processing or timeout (currently hardcoded at 10 minutes).

For knowledge base that has auto_schedule set to False, the document source will be 
processed immediately. The function will return after the document source is processed.
"""

    @classmethod
    def depends_on(cls) -> List[Type["FlowComponent"]]:
        return [WebSearcher]

    @classmethod
    def direct_flow_option_items(cls) -> List[FlowOptionItem]:
        return AbstractStep.get_flow_option_items() + [
            flow_option_items.FOI_RETRIEVER(explicit=True),
            flow_option_items.FOI_DAYS_LIMIT(explicit=True),
            flow_option_items.FOI_SEARCH_MAX_RESULTS(explicit=True),
        ]

    @staticmethod
    def run_step(
        exec_info: ExecInfo, search_keywords: Optional[str] = None
    ) -> DocSource:
        """
        Create a document source with web search.

        If a scheduler is running, the document source will be scheduled for processing.
        Otherwise, the scheduler will be started to process the document source. The actual
        web seacher will be started by the scheduler using the config in the exec_info.

        This function will wait for the document source to finish processing or timeout (
        currently hardcoded at 10 minutes).

        Args:
        - exec_info: Execution information
        - search_keywords: The search keywords. If not provided, the original query
          from the chat_query_item will be used.

        Returns:
        -  The docsource created
        """

        context = exec_info.context
        org = exec_info.org
        kb = exec_info.kb
        docsource_store = context.get_repo_manager().get_docsource_store()

        display_logger = exec_info.display_logger
        if display_logger is None:
            display_logger = logger()

        if exec_info.target_chat_query_item is None:
            raise exceptions.UnexpectedCaseException(
                "The chat query item is not provided."
            )

        if search_keywords is None:
            search_keywords = exec_info.target_chat_query_item.query_content

        display_logger.info(f"Searching the web for query: {search_keywords}")

        docsource = _create_docsrc_for_search(
            exec_info=exec_info,
            search_keywords=search_keywords,
        )

        if exec_info.kb.auto_schedule:
            if exec_info.context.scheduler_is_running:
                display_logger.info("Scheduled the new DocSource to be processed ...")
                started = False
            else:
                display_logger.info(
                    "Start the scheduler to process the new DocSource ..."
                )
                started = run_scheduler(context=context)

            # TODO next: we should let the caller to check the docsource status
            if started == False:
                # another process is running the scheduler
                finished = docsource_store.wait_for_docsource(
                    org, kb, docsource, timeout_in_secs=300
                )
                if finished == False:
                    display_logger.warning(
                        "The document source has not finished processing yet."
                    )
                else:
                    display_logger.info("The document source has finished processing.")
                    docsource.source_status = DocSourceStatus.COMPLETED
                    docsource_store.update_docsource(org, kb, docsource)
            else:
                # the scheduler has been started and finished processing
                pass
            return docsource

        display_logger.info("[Status]Start the document process pipeline ...")
        try:
            # if the kb.auto_schedule is False, we should run the process manually
            success_documents = _run_web_search_pipeline(
                exec_info=exec_info, docsource=docsource
            )
            display_logger.info(
                f"Successfully processed {len(success_documents)} documents."
            )
            return docsource
        except Exception as e:
            display_logger.error(f"Failed to run the web search pipeline: {e}")
            docsource.source_status = DocSourceStatus.FAILED
            docsource_store.update_docsource(org, kb, docsource)
            raise e


def _run_web_search_pipeline(
    exec_info: ExecInfo, docsource: DocSource
) -> List[Document]:

    # this is basically the logic from the scheduler
    context = exec_info.context
    display_logger = exec_info.display_logger
    org = exec_info.org
    kb = exec_info.kb
    query = exec_info.target_chat_query_item.query_content
    user = exec_info.user
    flow_options = exec_info.target_chat_query_item.chat_query_options.flow_options
    docsource_store = context.get_repo_manager().get_docsource_store()

    web_searcher = WebSearcher(context=context)

    docsink_create_list = web_searcher.create_docsinks_by_search_and_scrape(
        context=context,
        org=org,
        kb=kb,
        user=user,
        query=query,
        docsource=docsource,
        flow_options=flow_options,
        display_logger=display_logger,
    )

    if docsink_create_list is None or len(docsink_create_list) == 0:
        display_logger.warning(f"No results found for the query {query}.")
        docsource.source_status = DocSourceStatus.COMPLETED
        docsource_store.update_docsource(org, kb, docsource)
        return []

    success_documents = pipeline_utils.run_adhoc_pipeline_for_docsinks(
        exec_info=exec_info, docsink_create_list=docsink_create_list
    )
    docsource.source_status = DocSourceStatus.COMPLETED
    docsource_store.update_docsource(org, kb, docsource)
    return success_documents


def _create_docsrc_for_search(
    exec_info: ExecInfo,
    search_keywords: str,
) -> DocSource:

    context = exec_info.context
    org = exec_info.org
    kb = exec_info.kb
    docsource_store = context.get_repo_manager().get_docsource_store()

    chat_query_options = exec_info.chat_query_options
    chat_query_item = exec_info.target_chat_query_item

    display_logger = exec_info.display_logger
    if display_logger is None:
        display_logger = logger()

    flow_options = chat_query_options.flow_options
    if flow_options is None:
        flow_options = {}

    retriever_type = config_utils.get_str_option_value(
        options=flow_options,
        option_name=flow_option.FLOW_OPTION_RETRIEVER_TYPE,
        default_value=RetrieverType.GOOGLE,
        display_logger=display_logger,
    )

    days_limit, max_results = search_utils.get_common_search_paras(
        flow_options=flow_options,
        settings=exec_info.context.settings,
        display_logger=display_logger,
    )

    display_logger.info(f"[Status]Searching the web with {retriever_type} ...")

    # use yyyy-mm-dd-hh-mm-ss to the URI to distinguish different searches
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    docsource_create = DocSourceCreate(
        kb_id=kb.kb_id,
        source_type=DocSourceType.SEARCH,
        uri=(
            f"search://{retriever_type}?q={search_keywords}&date_range={days_limit}"
            f"&max_results={max_results}&ts={timestamp}"
        ),
        display_name=search_keywords,
        ingest_config=IngestConfig(
            flow_options=flow_options,
            extra_parameters={
                "chat_id": chat_query_item.chat_id,
                "query_id": chat_query_item.query_id,
            },
        ),
    )
    docsource = docsource_store.create_docsource(org, kb, docsource_create)
    return docsource