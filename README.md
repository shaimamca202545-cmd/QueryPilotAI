# QueryPilot AI — AI-Powered SQL Database Engineer

Ask your MySQL database questions (and give it instructions) in plain English.
QueryPilot inspects your live schema, asks a local **Ollama llama3.2** model to
write the MySQL, runs it, and turns the result back into a natural-language
answer — with the generated SQL and raw result table available underneath as
optional, collapsible details.

```
User question → FastAPI → live schema → Ollama (llama3.2) → SQL
→ execute on MySQL → result → Ollama → human-language answer
```

Supports `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, `DROP`,
`TRUNCATE`, joins, aggregates, `GROUP BY` / `HAVING`, subqueries and more —
this is a full database engineer, not a read-only assistant. Follow-up
questions ("what's their average salary?") are understood using recent
conversation history.

---

## 1. Prerequisites

- Python 3.10+
- A running MySQL server (5.7+/8.0+) with a database you want to query
- [Ollama](https://ollama.com) installed locally, with the `llama3.2` model pulled

## 2. Install Ollama and pull the model

```bash
# macOS
brew install ollama
# or download the installer from https://ollama.com/download for your OS

# Start the Ollama server (leave this running in its own terminal)
ollama serve

# In another terminal, pull the model this app uses
ollama pull llama3.2

# Sanity check
ollama list
```

## 3. Configure the database connection

```bash
cd queextpilot
cp .env.example .env
```

Edit `.env` and fill in your real MySQL credentials — nothing is hard-coded
in the code:

```
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password_here
MYSQL_DATABASE=company_db

OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

MAX_RESULT_ROWS=200
ALLOW_WRITE_QUERIES=true
```

Set `ALLOW_WRITE_QUERIES=false` if you ever want to lock the assistant down
to read-only `SELECT` queries only.

If you don't have a sample database yet, create a quick one to try it on:

```sql
CREATE DATABASE company_db;
USE company_db;

CREATE TABLE departments (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(100) NOT NULL
);

CREATE TABLE employees (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(100) NOT NULL,
  department_id INT,
  salary DECIMAL(10,2),
  hired_on DATE,
  FOREIGN KEY (department_id) REFERENCES departments(id)
);

INSERT INTO departments (name) VALUES ('IT'), ('Sales'), ('HR');

INSERT INTO employees (name, department_id, salary, hired_on) VALUES
 ('Rahul', 1, 85000, '2021-02-01'),
 ('Priya', 1, 82000, '2020-11-15'),
 ('Amit', 2, 79000, '2019-06-10'),
 ('Sneha', 2, 76000, '2022-01-20'),
 ('Rohan', 3, 74000, '2021-09-05');
```

## 4. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## 5. Run the app

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser. The header shows a live
"● Connected" indicator once both MySQL and Ollama are reachable.

---

## Project structure

```
queextpilot/
  app/
    main.py             FastAPI app + endpoints (/, /chat, /schema, /health)
    database.py          MySQL pool, statement execution, error handling
    schema.py             INFORMATION_SCHEMA introspection + caching
    sql_generator.py       Ollama client: SQL generation + answer synthesis
    sql_validator.py        Safety checks on generated SQL
    result_formatter.py      Row/type-safe formatting for UI + LLM prompts
    models.py                 Pydantic request/response schemas
    prompts.py                  Prompt templates sent to Ollama
  templates/index.html   Chat workspace UI
  static/style.css        Styling (dark/light theme, responsive)
  static/script.js         Frontend logic (fetch, rendering, sessions)
  requirements.txt
  .env.example
```

## Endpoints

| Method | Path       | Purpose                                             |
|--------|-----------|------------------------------------------------------|
| GET    | `/`        | Serves the chat UI                                  |
| POST   | `/chat`     | `{message, session_id}` → natural-language answer + SQL + result |
| GET    | `/schema`    | Live table/column/PK/FK schema (JSON)              |
| GET    | `/health`     | MySQL + Ollama connectivity status                |
| POST   | `/reset/{id}`  | Clears a conversation's history                  |

## How a request flows through the app

1. **`/chat`** receives the user's message and a `session_id`.
2. `schema.py` inspects (and caches for 60s) the live database structure.
3. `prompts.py` + `sql_generator.py` ask **llama3.2** for one SQL statement,
   grounded in the real schema and recent conversation turns.
4. `sql_validator.py` rejects empty output, stacked statements, and
   server/file-system-level operations (`INTO OUTFILE`, `LOAD_FILE`, `GRANT`,
   etc.) before anything reaches MySQL.
5. `database.py` executes the statement inside a transaction, commits on
   success, and rolls back and returns a structured error on failure —
   never crashes the process.
6. On a MySQL error, the app asks Ollama once to look at the error and
   propose a corrected statement, then retries.
7. On success, `result_formatter.py` builds a compact preview and
   `sql_generator.py` asks Ollama to turn it into a natural-language answer.
8. The API returns `{reply, sql, columns, rows, row_count, ...}`. The
   frontend always renders `reply` as the primary message; `sql` and the
   result table are rendered as collapsible sections underneath.

## Notes on safety

This app is intentionally allowed to run write and DDL statements
(`INSERT`/`UPDATE`/`DELETE`/`CREATE`/`ALTER`/`DROP`/`TRUNCATE`) since it's
meant to act as a full database engineer, not a read-only reporting tool.
The validator still blocks statement-stacking (`; DROP TABLE ...` appended
to a legitimate query), file-system access functions, and
privilege-management statements (`GRANT`/`REVOKE`/`CREATE USER`). For a
production deployment you would typically also run this against a
database user with only the privileges you want the assistant to have, and
consider point-in-time backups given it can execute `DROP`/`TRUNCATE`.

## Troubleshooting

- **"● Ollama offline"** — make sure `ollama serve` is running and
  `ollama pull llama3.2` has completed. Check `OLLAMA_HOST` in `.env`.
- **"● MySQL offline"** — check `MYSQL_HOST`/`PORT`/`USER`/`PASSWORD`/
  `DATABASE` in `.env`, and that the MySQL server is running and reachable.
- **Model writes bad SQL** — QueryPilot automatically retries once with the
  MySQL error fed back to the model; if it still can't produce valid SQL it
  will explain that in plain language instead of crashing.
