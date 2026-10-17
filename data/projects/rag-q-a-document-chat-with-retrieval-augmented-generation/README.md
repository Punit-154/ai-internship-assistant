# 📘 Textbook Q&A RAG System

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://rag-textbook-qa-demo.streamlit.app)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Groq](https://img.shields.io/badge/Groq-Llama_3.1-f55036?style=flat)](https://groq.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Welcome! This project is a Retrieval-Augmented Generation (RAG) application designed to let users upload PDF documents and ask specific questions about their content. Every answer is grounded in retrieved chunks from the document via an explicit prompt instruction, and a secondary LLM-as-a-judge call scores how faithfully each answer sticks to that retrieved context — making hallucination visible and measurable rather than claiming to eliminate it entirely. See `LIMITATIONS.md` for known failure modes, including cases where weak retrieval affects answer relevance.

---

## 🌐 Live Demo

Try it out yourself! Upload a PDF and start chatting:  
**👉 [Launch Frontend Demo](https://ragq-a-3na9rwvkkwv6djq9iqjuau.streamlit.app/)**

---

## ⚙️ How It Works

The system is built on a decoupled frontend-backend architecture that processes documents and queries in real-time.

1. **Ingestion & Parsing**: When a PDF is uploaded via the Streamlit UI, the FastAPI backend immediately parses the text.
2. **Semantic Chunking**: The text is split into logical segments (default: 800 characters) with a slight overlap (100 characters) to ensure no context is lost mid-sentence.
3. **Embedding**: Each chunk is passed through a local HuggingFace embedding model (`all-MiniLM-L6-v2`) to generate semantic vectors.
4. **Storage**: The vectors are stored in a rapid, in-memory Chroma database.
5. **Conversational Query Rewriting**: Follow-up questions are automatically rewritten by the LLM in the background to resolve pronouns (e.g., changing "explain it more" to "explain the concept of photosynthesis more") so the vector search remains highly accurate to context.
6. **Retrieval**: The system queries ChromaDB using the rewritten search query to retrieve the top $K$ most relevant document chunks.
7. **Generation**: The retrieved chunks, conversation history, and the user's question are compiled into a grounded prompt. The Groq API (running `llama-3.1-8b-instant`) synthesizes a precise answer based *only* on the provided context.
8. **LLM-as-a-Judge Faithfulness Verification**: Post-generation, the backend runs a verification prompt comparing the answer to the retrieved context to calculate a support/faithfulness score (0-100%).

---

## 🚀 Metrics & Advanced Capabilities

This pipeline is optimized for transparency, safety, and precision:

*   ⚡ **Sub-Second Generation**: By leveraging Groq's specialized inference hardware, the Llama-3.1 model generates complete answers in under 1 second.
*   🧠 **Semantic Retrieval with Measured Quality**: The `all-MiniLM-L6-v2` embedding model performs vector similarity search, and each response reports a live Match Score (derived from Chroma's L2 distance) alongside a Faithfulness Score, so retrieval and generation quality are observable per-query rather than assumed. See `LIMITATIONS.md` for documented cases where retrieval quality degrades (e.g. enumeration-style questions, meta-level questions about the whole document, and poorly-extracted formula/table content).
*   🛡️ **Strict Hallucination Prevention**: The prompt strictly enforces that the LLM must respond with "I don't know based on the provided document" if the answer cannot be found in the retrieved chunks, avoiding factual generation.
*   ⚖️ **Resource Efficiency**: Heavy embedding computation is handled locally, while the heavy generative lifting is offloaded to Groq's API.
*   📊 **Real-Time Observability**: The system returns detailed telemetry metrics displayed directly in the UI under response metadata:
    *   **Retrieval Latency**: The exact database search lookup time in milliseconds.
    *   **LLM Latency**: Groq API generation and round-trip time in milliseconds.
    *   **Match Score**: ChromaDB distance metrics normalized to a similarity percentage.
    *   **Faithfulness Evaluation**: Real-time validation percentage scoring if the output is strictly supported by the document source.
*   🚦 **Backend Safety Guardrails**: Restricts files to PDF format and a 20MB limit to prevent memory exhaustion and processing overflows.

---

## 🛠️ Tech Stack

*   **UI/Frontend**: Streamlit
*   **Backend Framework**: FastAPI
*   **Vector Database**: ChromaDB (In-memory)
*   **Embeddings**: `all-MiniLM-L6-v2` (via HuggingFace/Langchain)
*   **LLM Engine**: Groq (`llama-3.1-8b-instant`)

---

## 💻 For Developers: Running Locally

If you'd like to fork this project and run it yourself:

1. Clone the repository and install dependencies: `pip install -r requirements.txt`
2. Create a `.env` file with your `GROQ_API_KEY`.
3. Start the backend: `uvicorn app:app --port 8000`
4. Start the frontend: `streamlit run streamlit_app.py --server.port 8501`
