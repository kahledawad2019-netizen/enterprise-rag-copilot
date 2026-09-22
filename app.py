r"""
Streamlit chat UI.

Run:  .venv\Scripts\streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from src.security import UnsafeSQLError
from src.settings import get_settings
from src.vanna_agent import build_vanna

st.set_page_config(page_title="SQL Server RAG", page_icon="::", layout="wide")


@st.cache_resource(show_spinner="Connecting to SQL Server and Ollama ...")
def load_agent():
    settings = get_settings()
    return build_vanna(settings), settings


vn, settings = load_agent()

# --------------------------------------------------------------------------
# Sidebar - status only. Never render a secret here.
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("Configuration")
    st.text(f"Server    {settings.mssql_server}")
    st.text(f"Database  {settings.mssql_database}")
    st.text(f"Auth      {settings.mssql_auth_mode}")
    st.text(f"Model     {settings.ollama_model}")
    st.text(f"Host      {settings.ollama_host}")

    st.divider()
    st.header("Safety")
    st.text(f"Read-only   {settings.read_only}")
    st.text(f"Max rows    {settings.max_rows}")
    st.text(f"LLM sees data {settings.allow_llm_to_see_data}")

    st.divider()
    if st.button("Show training corpus"):
        st.session_state["show_training"] = True
    if st.button("Clear chat"):
        st.session_state["messages"] = []
        st.rerun()

if st.session_state.pop("show_training", False):
    st.subheader("Training data in the vector store")
    st.dataframe(vn.get_training_data(), use_container_width=True)

# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
st.title("Ask your database")
st.caption("Vanna AI + ChromaDB + local llama3.1 via Ollama. Nothing leaves this machine.")

st.session_state.setdefault("messages", [])

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

question = st.chat_input("e.g. How many orders were placed last month?")

if question:
    st.session_state["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Generating SQL ..."):
            try:
                sql = vn.generate_sql(
                    question, allow_llm_to_see_data=settings.allow_llm_to_see_data
                )
            except Exception as exc:
                st.error(f"Could not generate SQL: {exc}")
                st.stop()

        st.code(sql, language="sql")

        try:
            with st.spinner("Running query ..."):
                df = vn.run_sql(sql)
        except UnsafeSQLError as exc:
            st.error(f"Blocked by the read-only guardrail: {exc}")
            st.stop()
        except Exception as exc:
            st.error(f"Query failed: {exc}")
            st.stop()

        st.dataframe(df, use_container_width=True)
        st.caption(f"{len(df)} rows")

        # Chart, when the shape of the result makes one meaningful.
        if len(df) > 1 and len(df.columns) > 1:
            try:
                code = vn.generate_plotly_code(question=question, sql=sql, df=df)
                fig = vn.get_plotly_figure(plotly_code=code, df=df)
                st.plotly_chart(fig, use_container_width=True)
            except Exception:
                pass  # a chart is a bonus, never a failure

        with st.expander("Was this correct? Teach the model"):
            st.write(
                "If the SQL above is right, save it as a training example so "
                "similar questions get better answers."
            )
            if st.button("Save as a correct example"):
                vn.train(question=question, sql=sql)
                st.success("Added to the training corpus.")

        st.session_state["messages"].append(
            {"role": "assistant", "content": f"```sql\n{sql}\n```\n\n{len(df)} rows returned."}
        )
