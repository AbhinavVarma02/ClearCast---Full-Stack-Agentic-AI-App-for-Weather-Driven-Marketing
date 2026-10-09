"""LangGraph tool-calling agent for ClearCast.

Architecture : add_messages state, an LLM chatbot node,
ToolNode execution, tools_condition routing, and MemorySaver checkpointing.
The loop is START -> chatbot -> tools -> chatbot until the LLM returns an answer.

Each user session gets its own ``thread_id`` so MemorySaver checkpoints never
mix conversations. MemorySaver is process-local: history is lost on restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError
from typing_extensions import TypedDict

from agent.errors import ToolServiceUnavailableError
from agent.prompts import get_system_prompt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

# Bounds LangGraph super-steps (each chatbot or tools node execution counts),
# so a model that keeps requesting tools cannot run an open-ended number of
# paid LLM calls. It is not a guarantee of exactly 10 tool calls.
RECURSION_LIMIT = 10


class State(TypedDict):
    """Conversation state accumulated by the add_messages reducer."""

    # The reducer appends node updates instead of replacing message history.
    messages: Annotated[list, add_messages]


def thread_id_for(session_id: str) -> str:
    """Map a server-generated session identifier to its LangGraph thread."""
    if not session_id:
        raise ValueError("A session identifier is required; shared threads are not allowed.")
    return f"session:{session_id}"


def graph_config(session_id: str) -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id_for(session_id)},
        "recursion_limit": RECURSION_LIMIT,
    }


def select_context_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Keep earlier briefs and answers, but only the current turn's tool traffic.

    Earlier turns' tool payloads are large and must not count as evidence for a
    new brief; dropping them also guarantees no orphaned tool-call messages
    (for example after a turn hit the recursion limit) reach the provider.
    """
    last_human = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=0,
    )
    earlier = [
        message
        for message in messages[:last_human]
        if isinstance(message, HumanMessage) or (isinstance(message, AIMessage) and not message.tool_calls)
    ]
    return earlier + list(messages[last_human:])


def handle_tool_error(exc) -> str:  # unannotated: ToolNode infers handled types from hints
    """Turn tool failures into error ToolMessages the model and evidence layer can read.

    Provider and transport failures become ``status="error"`` ToolMessages;
    anything else (a programming error) propagates instead of being hidden.
    """
    if isinstance(exc, ToolInvocationError):
        return f"Error: [invalid_tool_arguments] {exc.message}"
    if isinstance(exc, ToolServiceUnavailableError):
        return f"Error: [tool_service_unavailable] {exc.message}"
    if isinstance(exc, ToolException | asyncio.TimeoutError):
        return f"Error: {exc}"
    raise exc


def build_graph_from_tools(tools: list, llm, *, checkpointer=None):
    """Compile the chatbot/ToolNode loop for the given tools and chat model."""
    # Step 2: create the graph builder after defining State.
    graph_builder = StateGraph(State)

    # Step 3: bind discovered schemas so the model can select MCP tools.
    llm_with_tools = llm.bind_tools(tools)

    async def chatbot(state: State, config: RunnableConfig):
        """Run the analyst with its stable system prompt and current history."""
        system_message = SystemMessage(content=get_system_prompt())
        context = select_context_messages(state["messages"])
        response = await llm_with_tools.ainvoke([system_message, *context], config)
        return {"messages": [response]}

    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", ToolNode(tools=tools, handle_tool_errors=handle_tool_error))

    # Step 4: tools_condition ends the graph when there are no tool calls.
    graph_builder.add_conditional_edges("chatbot", tools_condition)
    graph_builder.add_edge("tools", "chatbot")
    graph_builder.add_edge(START, "chatbot")

    # Step 5: thread_id-scoped checkpoints provide per-session conversation memory.
    return graph_builder.compile(checkpointer=checkpointer or MemorySaver())


async def build_graph(*, mcp_client=None, llm=None, checkpointer=None):
    """Discover MCP tools and compile the agent graph."""
    from agent.llm import create_chat_model
    from agent.weather_client import get_langchain_tools

    tools = await get_langchain_tools(mcp_client)
    return build_graph_from_tools(tools, llm or create_chat_model(), checkpointer=checkpointer)


async def ainvoke_graph(graph, user_message: str, *, session_id: str, message_id: str | None = None) -> dict:
    """Invoke the compiled graph for one session and return the final state."""
    message = HumanMessage(content=user_message, id=message_id) if message_id else HumanMessage(user_message)
    return await graph.ainvoke({"messages": [message]}, config=graph_config(session_id))


def invoke_graph(graph, user_message: str, *, thread_id: str) -> str:
    """Synchronously invoke the graph and return its final natural-language answer.

    ``thread_id`` is required: defaulting every caller to one shared thread
    would mix different users' conversation history.
    """
    result = asyncio.run(ainvoke_graph(graph, user_message, session_id=thread_id))
    return result["messages"][-1].content
