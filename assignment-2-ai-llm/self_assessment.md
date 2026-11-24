# Question 1 – Self-assessment

**Scale:** A = can code independently · B = can code under supervision · C = little or no understanding.

| Area | Rating |
|---|---|
| LLM | **A** |
| Deep Learning | **A** |
| AI | **A** |
| Machine Learning | **A** |

I can independently design, code and ship work in all four areas. The ratings below are backed by practical examples, several of which can be run in this repository.

---

### LLM – A

I build LLM applications end to end, independently. That covers:

- prompt design with structured (JSON-schema) outputs and function/tool calling,
- RAG pipelines (chunking, embeddings, hybrid vector search, re-ranking),
- agent workflows with MCP-style tool integration, and
- the async API services around them.

In the GhostAgent / DataRipple contact-centre platform, I worked on LLM-based extraction of contact details, requirements and intent from live call transcripts. That output was validated and pushed into 20+ CRMs at more than 1,000 interactions a day. This repository shows the same engineering habits:

- typed extraction contracts,
- treating model output as untrusted, and
- guardrails against prompt injection and data leakage (see `llm_chatbot_architecture.md`).

### Deep Learning – A

I independently build, train and fine-tune neural networks in PyTorch and TensorFlow:

- CNNs for vision, RNN/LSTM models for sequences, and
- transformer models (attention, embeddings, fine-tuning pre-trained encoders and LLMs).

I handle the full training loop myself:

- data pipelines, loss and optimiser choice, learning-rate schedules,
- regularisation, overfitting diagnosis,
- evaluation, and
- inference optimisation for deployment.

### AI – A

I design and deliver complete applied AI systems that combine:

- LLMs, retrieval, classical ML and deterministic rules engines,
- external APIs, and
- asynchronous orchestration, error handling, monitoring and human-in-the-loop controls.

The GhostAgent compliance pipeline is a production example. It contributed to 35% fewer compliance violations and 40% lower QA review time. I also design AI-specific security controls against prompt injection, retrieval poisoning and excessive agent permissions, as shown in this submission.

### Machine Learning – A

I independently run the full ML workflow with scikit-learn, pandas and NumPy:

- data cleaning and feature engineering,
- training and tuning models (linear/logistic regression, decision trees, random forests, gradient boosting, SVM, clustering),
- rigorous evaluation (precision/recall, F1, ROC-AUC, cross-validation, leakage checks), and
- packaging models behind APIs with Docker and MLOps practices (versioning, monitoring, retraining).
