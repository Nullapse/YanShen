import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gongkao.db import CURRENT_SCHEMA_VERSION
from gongkao.taxonomy import QUESTION_TYPES, classify_question_type

EXPECTED = {"papers": 455, "questions": 1750, "reference_answers": 9410}
PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:\\|/Users/|/home/)")
ALLOWED_QUESTION_TYPES = set(QUESTION_TYPES)


def audit_database(path):
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"schema version: expected {CURRENT_SCHEMA_VERSION}, got {version}"
            )
        for table, expected in EXPECTED.items():
            actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"{table}: expected {expected}, got {actual}")
        checks = {
            "questions without answers": "SELECT COUNT(*) FROM questions q WHERE NOT EXISTS (SELECT 1 FROM reference_answers r WHERE r.question_id = q.id)",
            "papers without questions": "SELECT COUNT(*) FROM papers p WHERE NOT EXISTS (SELECT 1 FROM questions q WHERE q.paper_id = p.id)",
            "papers without materials": "SELECT COUNT(*) FROM papers p WHERE NOT EXISTS (SELECT 1 FROM paper_materials m WHERE m.paper_id = p.id AND TRIM(COALESCE(m.content, '')) <> '')",
            "placeholder answers": "SELECT COUNT(*) FROM reference_answers WHERE TRIM(COALESCE(answer_text, '')) IN ('', '暂无答案数据') OR TRIM(COALESCE(organization, '')) IN ('', '??????')",
            "stored API keys": "SELECT COUNT(*) FROM ai_settings WHERE api_key <> ''",
            "attempts": "SELECT COUNT(*) FROM attempts",
            "grading reports": "SELECT COUNT(*) FROM grading_reports",
            "grading rubrics": "SELECT COUNT(*) FROM grading_rubrics",
            "grading jobs": "SELECT COUNT(*) FROM grading_jobs",
            "grading report contexts": "SELECT COUNT(*) FROM grading_report_contexts",
            "grading feedback": "SELECT COUNT(*) FROM grading_feedback",
            "question favorites": "SELECT COUNT(*) FROM question_favorites",
            "paper favorites": "SELECT COUNT(*) FROM paper_favorites",
            "text annotations": "SELECT COUNT(*) FROM text_annotations",
            "imports": "SELECT COUNT(*) FROM imports",
            "agent messages": "SELECT COUNT(*) FROM agent_messages",
            "agent memories": "SELECT COUNT(*) FROM agent_memories",
            "agent conversations": "SELECT COUNT(*) FROM agent_conversations",
            "training plan items": "SELECT COUNT(*) FROM training_plan_items",
            "agent feedback": "SELECT COUNT(*) FROM agent_feedback",
            "agent steps": "SELECT COUNT(*) FROM agent_steps",
            "agent runs": "SELECT COUNT(*) FROM agent_runs",
            "agent evaluations": "SELECT COUNT(*) FROM agent_eval_results",
            "agent weakness profiles": "SELECT COUNT(*) FROM agent_weakness_profile",
            "answers without organization groups": "SELECT COUNT(*) FROM reference_answers WHERE canonical_organization = ''",
            "private agent context chunks": "SELECT COUNT(*) FROM agent_context_chunks WHERE source_type IN ('attempt', 'grading_report', 'personal_note')",
            "non-public knowledge chunks": "SELECT COUNT(*) FROM agent_context_chunks WHERE source_type = 'knowledge' AND json_extract(metadata_json, '$.visibility') <> 'public'",
        }
        for label, sql in checks.items():
            count = conn.execute(sql).fetchone()[0]
            if count:
                raise RuntimeError(f"release database contains {count} {label}")
        if conn.execute("SELECT COUNT(*) FROM agent_context_chunks").fetchone()[0]:
            raise RuntimeError("release database contains a prebuilt retrieval index")
        if conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0]:
            raise RuntimeError("release database contains pending retrieval-index work")
        derived_tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND (name = 'agent_context_fts' OR name LIKE 'agent_context_vec_%')"
            )
        ]
        if derived_tables:
            raise RuntimeError(f"release database contains derived retrieval tables: {derived_tables}")
        embedding_state = conn.execute(
            "SELECT embedding_model, embedding_dimensions FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        if tuple(embedding_state) != ("feature-hash-v1", 128):
            raise RuntimeError(f"release embedding state is not portable: {tuple(embedding_state)}")
        index_flags = conn.execute(
            "SELECT dirty, full_rebuild FROM agent_context_index_state WHERE id = 1"
        ).fetchone()
        if tuple(index_flags) != (1, 1):
            raise RuntimeError(f"release retrieval index should be deferred: {tuple(index_flags)}")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("release database failed PRAGMA quick_check")

        invalid_types = [
            row[0]
            for row in conn.execute("SELECT DISTINCT question_type FROM questions")
            if row[0] not in ALLOWED_QUESTION_TYPES
        ]
        if invalid_types:
            raise RuntimeError(f"release database contains invalid question types: {invalid_types}")
        taxonomy_errors = []
        for row_id, question_type, prompt, requirements in conn.execute(
            "SELECT id, question_type, prompt, requirements FROM questions"
        ):
            inferred, reason = classify_question_type(
                prompt, requirements, fallback=question_type
            )
            if reason == "未命中明确规则" or inferred != question_type:
                taxonomy_errors.append((row_id, question_type, inferred, reason))
        if taxonomy_errors:
            raise RuntimeError(
                f"release database contains {len(taxonomy_errors)} taxonomy mismatches; "
                f"first: {taxonomy_errors[0]}"
            )
        if conn.execute("SELECT COUNT(*) FROM questions WHERE question_type IN ('申论题', '贯彻执行', '文章写作')").fetchone()[0]:
            raise RuntimeError("release database contains legacy question types")
        if conn.execute("SELECT COUNT(*) FROM questions WHERE UPPER(question_code) LIKE 'SRNZ-%'").fetchone()[0]:
            raise RuntimeError("release database contains legacy SRNZ question codes")
        if conn.execute("SELECT COUNT(*) FROM papers WHERE region = '新疆兵团'").fetchone()[0]:
            raise RuntimeError("release database contains Xinjiang Bingtuan as a separate region")
        if conn.execute("SELECT COUNT(*) FROM papers WHERE paper_name LIKE '%广东省公考%' AND paper_name LIKE '%公安%' AND (region <> '广东' OR exam_type <> '广东省考')").fetchone()[0]:
            raise RuntimeError("release database misclassifies Guangdong police papers")
        if conn.execute("SELECT COUNT(*) FROM papers WHERE paper_name LIKE '%公安院校联考%' AND exam_type <> '公安院校联考'").fetchone()[0]:
            raise RuntimeError("release database misclassifies public security academy joint exam papers")

        text_columns = [
            ("questions", "source_note"),
            ("question_sources", "source_path"),
            ("reference_answers", "notes"),
        ]
        for table, column in text_columns:
            for (value,) in conn.execute(f"SELECT {column} FROM {table} WHERE {column} <> ''"):
                if PRIVATE_PATH.search(value or ""):
                    raise RuntimeError(f"private absolute path found in {table}.{column}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Audit the public seed database.")
    parser.add_argument("path", nargs="?", default="data/gongkao_seed.sqlite3")
    args = parser.parse_args()
    audit_database(Path(args.path))
    print("Release database audit passed.")


if __name__ == "__main__":
    main()
