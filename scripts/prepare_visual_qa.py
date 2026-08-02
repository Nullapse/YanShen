import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".visual-qa" / "gongkao.sqlite3"
TARGET.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "data" / "gongkao_seed.sqlite3", TARGET)

question_ids = [563, 564, 565, 570, 567]
scores = [68, 76, 61, 82, 73]
answers = [
    "一是完善公共服务设施，补齐基层治理短板；二是健全群众参与机制，畅通意见表达渠道；三是强化数字技术赋能，提高服务精准度。",
    "新民风建设既是价值引领，也是治理方式创新。通过村规民约、典型示范和群众议事，把外在约束转化为内在认同，形成共建共治共享格局。",
    "关于推进社区适老化服务的工作建议：一、摸清需求，建立动态台账。二、整合资源，完善服务网络。三、加强监督，持续评估服务效果。",
    "问题在于机制不健全、主体协同不足、服务供给与群众需求存在偏差。建议明确权责清单，建立跨部门协作机制，并通过反馈评价及时优化。",
    "以治理之笔绘就民生底色。基层治理需要以人民需求为起点，以制度建设为保障，以数字技术为支撑，在共建共治共享中提升群众获得感。",
]

with sqlite3.connect(TARGET) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    now = datetime.now(timezone.utc)
    for index, (question_id, score, answer) in enumerate(zip(question_ids, scores, answers)):
        created_at = (now - timedelta(days=4 - index)).strftime("%Y-%m-%d %H:%M:%S")
        note = "复盘：下次先按主体分层，再压缩重复表述。" if index in (1, 2) else ""
        cursor = conn.execute(
            """
            INSERT INTO attempts(
                question_id, answer_text, word_count, personal_note,
                duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (question_id, answer, len(answer), note, 720 + index * 95, created_at),
        )
        if index < 4:
            report = f"""## 批改结论

总分：{score}/100

### 做得好的地方

要点能够回应题干，表达较为清楚。

### 下一步建议

继续加强层次划分，补充材料依据，压缩重复内容。"""
            conn.execute(
                """
                INSERT INTO grading_reports(
                    attempt_id, provider, model, report_text, status, created_at
                ) VALUES (?, 'visual-qa', 'demo', ?, 'ok', ?)
                """,
                (cursor.lastrowid, report, created_at),
            )
    conn.execute("INSERT OR IGNORE INTO question_favorites(question_id) VALUES (563)")
    paper_id = conn.execute("SELECT paper_id FROM questions WHERE id = 563").fetchone()[0]
    if paper_id:
        conn.execute("INSERT OR IGNORE INTO paper_favorites(paper_id) VALUES (?)", (paper_id,))

print(TARGET)
