import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gongkao.db import init_db
from gongkao.organizations import canonicalize_organization
from gongkao.taxonomy import QUESTION_TYPES, classify_question_type


EXPECTED = {"papers": 455, "questions": 1750, "reference_answers": 9410}
ALLOWED_QUESTION_TYPES = set(QUESTION_TYPES)


def normalize_release_taxonomy(conn):
    rows = conn.execute(
        "SELECT id, prompt, requirements, question_type FROM questions"
    ).fetchall()
    conn.executemany(
        "UPDATE questions SET question_type = ? WHERE id = ?",
        [
            (
                classify_question_type(
                    prompt, requirements, fallback=question_type
                )[0],
                row_id,
            )
            for row_id, prompt, requirements, question_type in rows
        ],
    )
    for table in ("papers", "questions"):
        conn.execute(
            f"""
            UPDATE {table}
               SET region = '新疆',
                   source_province = '新疆',
                   exam_type = '新疆省考'
             WHERE region = '新疆兵团' OR exam_type = '新疆兵团省考'
            """
        )
        conn.execute(
            f"""
            UPDATE {table}
               SET region = '广东',
                   source_province = '广东',
                   exam_type = '广东省考',
                   paper_category = CASE WHEN COALESCE(paper_category, '') = '' THEN '公安' ELSE paper_category END
             WHERE paper_name LIKE '%广东省公考%' AND paper_name LIKE '%公安%'
            """
        )
        conn.execute(
            f"""
            UPDATE {table}
               SET region = '全国',
                   source_province = '全国',
                   exam_type = '公安院校联考'
             WHERE paper_name LIKE '%公安院校联考%'
            """
        )


def remove_invalid_public_content(conn):
    conn.execute(
        """
        DELETE FROM reference_answers
         WHERE TRIM(COALESCE(answer_text, '')) IN ('', '暂无答案数据')
            OR TRIM(COALESCE(organization, '')) IN ('', '??????')
        """
    )
    conn.execute(
        """
        DELETE FROM questions
         WHERE NOT EXISTS (
               SELECT 1 FROM reference_answers r WHERE r.question_id = questions.id
         )
        """
    )
    conn.execute(
        """
        DELETE FROM papers
         WHERE NOT EXISTS (
               SELECT 1 FROM questions q WHERE q.paper_id = papers.id
         )
        """
    )
    conn.execute(
        """
        DELETE FROM paper_materials
         WHERE NOT EXISTS (
               SELECT 1 FROM papers p WHERE p.id = paper_materials.paper_id
         )
        """
    )


def remove_retrieval_artifacts(conn):
    """Remove derived search data that is rebuilt in each user's local database."""
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
    except Exception:
        pass

    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND (name = 'agent_context_fts' OR name LIKE 'agent_context_vec_%')"
        )
    ]
    # Drop virtual tables rather than merely deleting their rows: FTS/vec shadow
    # pages otherwise remain in the SQLite file until an expensive rebuild.
    for table in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute("DELETE FROM agent_skill_edges")
    conn.execute("DELETE FROM agent_skill_nodes")


def build_release_db(source, output):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.sqlite3")
    if temporary.exists():
        temporary.unlink()
    shutil.copy2(source, temporary)
    init_db(temporary)

    conn = sqlite3.connect(temporary)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        normalize_release_taxonomy(conn)
        remove_invalid_public_content(conn)
        conn.execute("DELETE FROM question_favorites")
        conn.execute("DELETE FROM paper_favorites")
        conn.execute("DELETE FROM text_annotations")
        conn.execute("DELETE FROM grading_feedback")
        conn.execute("DELETE FROM grading_report_contexts")
        conn.execute("DELETE FROM grading_jobs")
        conn.execute("DELETE FROM grading_rubrics")
        conn.execute("DELETE FROM grading_reports")
        conn.execute("DELETE FROM attempts")
        conn.execute("DELETE FROM imports")
        conn.execute("DELETE FROM agent_messages")
        conn.execute("DELETE FROM agent_memories")
        conn.execute("DELETE FROM agent_conversations")
        conn.execute("DELETE FROM training_plan_items")
        conn.execute("DELETE FROM agent_feedback")
        conn.execute("DELETE FROM agent_steps")
        conn.execute("DELETE FROM agent_runs")
        conn.execute("DELETE FROM agent_eval_results")
        conn.execute("DELETE FROM agent_weakness_profile")
        conn.execute("DELETE FROM agent_context_chunks")
        conn.execute("DELETE FROM agent_context_pending")
        remove_retrieval_artifacts(conn)
        conn.execute(
            """
            UPDATE ai_settings
               SET mode = 'api', provider_name = 'DeepSeek',
                   api_base_url = 'https://api.deepseek.com', api_key = '',
                   api_key_env = 'DEEPSEEK_API_KEY', model = 'deepseek-v4-pro',
                   temperature = 0.2, prompt_template = '', grading_mode = 'enhanced'
             WHERE id = 1
            """
        )
        rows = conn.execute("SELECT id, organization FROM reference_answers").fetchall()
        conn.executemany(
            "UPDATE reference_answers SET canonical_organization = ? WHERE id = ?",
            [(canonicalize_organization(name), row_id) for row_id, name in rows],
        )
        # The organization normalization above touches indexed source rows and
        # therefore queues work through database triggers. Clear that queue only
        # after all release-data edits are complete, so indexing is deferred to
        # the user's local database instead of being shipped in the archive.
        conn.execute("DELETE FROM agent_context_pending")
        # The retrieval index duplicates the full question bank and can grow the
        # distributable database by hundreds of megabytes. Keep the release
        # seed lean and let the app build this cache in the user's data folder
        # when AI coach retrieval is first used.
        conn.execute(
            "UPDATE agent_context_index_state "
            "SET dirty = 1, full_rebuild = 1, embedding_model = 'feature-hash-v1', "
            "embedding_dimensions = 128, knowledge_signature = '' WHERE id = 1"
        )
        conn.execute("DELETE FROM release_metadata")
        conn.execute(
            "INSERT INTO release_metadata (key, value) VALUES ('content_version', '1.3.6')"
        )
        conn.commit()

        for table, expected in EXPECTED.items():
            actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"{table}: expected {expected}, got {actual}")
        if conn.execute("SELECT COUNT(*) FROM questions q WHERE NOT EXISTS (SELECT 1 FROM reference_answers r WHERE r.question_id = q.id)").fetchone()[0]:
            raise RuntimeError("release database contains questions without answers")
        if conn.execute("SELECT COUNT(*) FROM reference_answers WHERE TRIM(COALESCE(answer_text, '')) IN ('', '暂无答案数据') OR TRIM(COALESCE(organization, '')) IN ('', '??????')").fetchone()[0]:
            raise RuntimeError("release database contains placeholder answers")
        if conn.execute("SELECT COUNT(*) FROM papers p WHERE NOT EXISTS (SELECT 1 FROM questions q WHERE q.paper_id = p.id)").fetchone()[0]:
            raise RuntimeError("release database contains papers without questions")
        if conn.execute("SELECT COUNT(*) FROM papers p WHERE NOT EXISTS (SELECT 1 FROM paper_materials m WHERE m.paper_id = p.id AND TRIM(COALESCE(m.content, '')) <> '')").fetchone()[0]:
            raise RuntimeError("release database contains papers without materials")
        if conn.execute("SELECT COUNT(*) FROM ai_settings WHERE api_key <> ''").fetchone()[0]:
            raise RuntimeError("release database contains an API key")
        if (
            conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM grading_reports").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM question_favorites").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM paper_favorites").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM text_annotations").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM grading_feedback").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM grading_report_contexts").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM grading_jobs").fetchone()[0]
            or conn.execute("SELECT COUNT(*) FROM grading_rubrics").fetchone()[0]
        ):
            raise RuntimeError("release database contains personal history")
        private_agent_tables = (
            "agent_messages",
            "agent_memories",
            "agent_conversations",
            "training_plan_items",
            "agent_feedback",
            "agent_steps",
            "agent_runs",
            "agent_eval_results",
            "agent_weakness_profile",
        )
        for table in private_agent_tables:
            if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise RuntimeError(f"release database contains private rows in {table}")
        if conn.execute("SELECT COUNT(*) FROM agent_context_chunks").fetchone()[0]:
            raise RuntimeError("release database contains a prebuilt retrieval index")
        if conn.execute("SELECT COUNT(*) FROM agent_context_pending").fetchone()[0]:
            raise RuntimeError("release database contains pending retrieval-index work")
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
        invalid_types = [
            row[0]
            for row in conn.execute("SELECT DISTINCT question_type FROM questions")
            if row[0] not in ALLOWED_QUESTION_TYPES
        ]
        if invalid_types:
            raise RuntimeError(f"release database contains invalid question types: {invalid_types}")
        if conn.execute("SELECT COUNT(*) FROM questions WHERE UPPER(question_code) LIKE 'SRNZ-%'").fetchone()[0]:
            raise RuntimeError("release database contains legacy SRNZ question codes")
        if conn.execute("SELECT COUNT(*) FROM papers WHERE region = '新疆兵团'").fetchone()[0]:
            raise RuntimeError("release database contains Xinjiang Bingtuan as a separate region")
        conn.execute("VACUUM")
    finally:
        conn.close()

    last_error = None
    for attempt in range(8):
        try:
            if output.exists():
                output.unlink()
            temporary.replace(output)
            last_error = None
            break
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return output


def main():
    parser = argparse.ArgumentParser(description="Build the sanitized public seed database.")
    parser.add_argument("--source", default="instance/gongkao.sqlite3")
    parser.add_argument("--output", default="data/gongkao_seed.sqlite3")
    args = parser.parse_args()
    result = build_release_db(args.source, args.output)
    print(result)


if __name__ == "__main__":
    main()
