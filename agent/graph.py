"""LangGraph tool-calling agent for ClearCast.

Architecture : add_messages state, an LLM chatbot node,
ToolNode execution, tools_condition routing, and MemorySaver checkpointing.
The loop is START -> chatbot -> tools -> chatbot until the LLM returns an answer.
"""

from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from agent.prompts import get_system_prompt
from agent.weather_client import get_langchain_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)


class State(TypedDict):
    """Conversation state accumulated by the add_messages reducer."""

    # The reducer appends node updates instead of replacing message history.
    messages: Annotated[list, add_messages]


async def build_graph():
    """Discover MCP tools and compile"""
    tools = await get_langchain_tools()

    # Step 2: create the graph builder after defining State.
    graph_builder = StateGraph(State)

    # Step 3: bind discovered schemas so the model can select MCP tools.
    llm = ChatOpenAI(model="gpt-4o-mini")
    llm_with_tools = llm.bind_tools(tools)

    def chatbot(state: State):
        """Run the analyst with its stable system prompt and current history."""
        system_message = {"role": "system", "content": get_system_prompt()}
        response = llm_with_tools.invoke([system_message, *state["messages"]])
        return {"messages": [response]}

    graph_builder.add_node("chatbot", chatbot)
    graph_builder.add_node("tools", ToolNode(tools=tools))

    # Step 4: tools_condition ends the graph when there are no tool calls.
    graph_builder.add_conditional_edges("chatbot", tools_condition)
    graph_builder.add_edge("tools", "chatbot")
    graph_builder.add_edge(START, "chatbot")

    # Step 5: thread_id-scoped checkpoints provide demo conversation memory.
    return graph_builder.compile(checkpointer=MemorySaver())


def invoke_graph(graph, user_message: str, thread_id: str = "1") -> str:
    """Invoke the compiled graph and return its final natural-language answer."""
    # Bound the chatbot/tool loop so a model that repeatedly requests tools
    # cannot generate an open-ended number of paid LLM calls.
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 10,
    }
    result = graph.invoke(
        {"messages": [{"role": "user", "content": user_message}]},
        config=config,
    )
    return result["messages"][-1].content
