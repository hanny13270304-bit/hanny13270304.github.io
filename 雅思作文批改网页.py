import sqlite3
import json
import os
import uuid
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, g
from urllib.request import Request, urlopen
from urllib.error import URLError

app = Flask(__name__, static_folder="static", static_url_path="")
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "essays.db")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_KEY = "sk-6d47b3a681194b639473a762a4ca9087"
os.makedirs(DB_DIR, exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS essays (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL CHECK(type IN ('task1','task2')),
            number TEXT NOT NULL DEFAULT '',
            question_text TEXT DEFAULT '',
            question_image TEXT DEFAULT '',
            model_essay TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS essay_versions (
            id TEXT PRIMARY KEY,
            essay_id TEXT NOT NULL,
            version_number INTEGER NOT NULL DEFAULT 1,
            content TEXT DEFAULT '',
            notes_a TEXT DEFAULT '',
            notes_b TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (essay_id) REFERENCES essays(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL UNIQUE,
            overall REAL,
            tr REAL, cc REAL, lr REAL, gra REAL,
            summary TEXT DEFAULT '',
            weaknesses_json TEXT DEFAULT '{}',
            revisions_json TEXT DEFAULT '[]',
            model_comparison_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (version_id) REFERENCES essay_versions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS annotations (
            id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            selected_text TEXT NOT NULL DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (version_id) REFERENCES essay_versions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS model_annotations (
            id TEXT PRIMARY KEY,
            essay_id TEXT NOT NULL,
            selected_text TEXT NOT NULL DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (essay_id) REFERENCES essays(id) ON DELETE CASCADE
        );
    """)
    db.commit()
    db.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Frontend ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── Essay CRUD ────────────────────────────────────────────

@app.route("/api/essays", methods=["GET"])
def list_essays():
    essay_type = request.args.get("type", "")
    keyword = request.args.get("keyword", "")
    db = get_db()
    query = """
        SELECT e.*,
               (SELECT a.overall FROM essay_versions v
                LEFT JOIN analyses a ON a.version_id = v.id
                WHERE v.essay_id = e.id ORDER BY v.version_number DESC LIMIT 1
               ) as latest_score
        FROM essays e WHERE 1=1
    """
    params = []
    if essay_type:
        query += " AND e.type = ?"
        params.append(essay_type)
    if keyword:
        query += " AND (e.number LIKE ? OR e.question_text LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    query += " ORDER BY e.updated_at DESC"
    rows = db.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/essays", methods=["POST"])
def create_essay():
    data = request.get_json()
    essay_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        """INSERT INTO essays (id, type, number, question_text, question_image, model_essay)
           VALUES (?,?,?,?,?,?)""",
        (essay_id, data.get("type", "task2"), data.get("number", ""),
         data.get("question_text", ""), data.get("question_image", ""),
         data.get("model_essay", "")))
    version_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO essay_versions (id, essay_id, version_number, content, notes_a, notes_b) VALUES (?,?,1,?,?,?)",
        (version_id, essay_id, data.get("content", ""), data.get("notes_a", ""), data.get("notes_b", "")))
    db.commit()
    return jsonify({"id": essay_id, "version_id": version_id}), 201


@app.route("/api/essays/<essay_id>", methods=["GET"])
def get_essay(essay_id):
    db = get_db()
    essay = db.execute("SELECT * FROM essays WHERE id=?", (essay_id,)).fetchone()
    if not essay:
        return jsonify({"error": "Not found"}), 404
    essay = dict(essay)
    versions = db.execute(
        """SELECT v.*, a.overall, a.tr, a.cc, a.lr, a.gra, a.summary,
                  a.weaknesses_json, a.revisions_json, a.model_comparison_json
           FROM essay_versions v
           LEFT JOIN analyses a ON a.version_id = v.id
           WHERE v.essay_id = ? ORDER BY v.version_number""",
        (essay_id,)).fetchall()
    essay["versions"] = []
    for v in versions:
        vd = dict(v)
        for f in ["weaknesses_json", "revisions_json", "model_comparison_json"]:
            raw = vd.pop(f, None)
            vd[f.replace("_json", "")] = json.loads(raw) if raw else ({} if "weak" in f else [])
        essay["versions"].append(vd)
    return jsonify(essay)


@app.route("/api/essays/<essay_id>", methods=["PUT"])
def update_essay(essay_id):
    data = request.get_json()
    db = get_db()
    db.execute(
        """UPDATE essays SET number=?, question_text=?, question_image=?,
           model_essay=?, updated_at=? WHERE id=?""",
        (data.get("number", ""), data.get("question_text", ""),
         data.get("question_image", ""), data.get("model_essay", ""),
         now_str(), essay_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/essays/<essay_id>", methods=["DELETE"])
def delete_essay(essay_id):
    db = get_db()
    db.execute("DELETE FROM essays WHERE id=?", (essay_id,))
    db.commit()
    return jsonify({"ok": True})


# ── Versions ──────────────────────────────────────────────

@app.route("/api/essays/<essay_id>/versions", methods=["POST"])
def add_version(essay_id):
    """Create a new version (rewrite), preserving the old one."""
    db = get_db()
    essay = db.execute("SELECT * FROM essays WHERE id=?", (essay_id,)).fetchone()
    if not essay:
        return jsonify({"error": "Not found"}), 404
    max_ver = db.execute(
        "SELECT MAX(version_number) as mv FROM essay_versions WHERE essay_id=?",
        (essay_id,)).fetchone()["mv"] or 0
    version_id = str(uuid.uuid4())[:8]
    data = request.get_json() or {}
    db.execute(
        "INSERT INTO essay_versions (id, essay_id, version_number, content, notes_a, notes_b) VALUES (?,?,?,?,?,?)",
        (version_id, essay_id, max_ver + 1, data.get("content", ""),
         data.get("notes_a", ""), data.get("notes_b", "")))
    db.execute("UPDATE essays SET updated_at=? WHERE id=?", (now_str(), essay_id))
    db.commit()
    return jsonify({"version_id": version_id, "version_number": max_ver + 1}), 201


@app.route("/api/versions/<version_id>", methods=["PUT"])
def update_version(version_id):
    data = request.get_json()
    db = get_db()
    db.execute(
        "UPDATE essay_versions SET content=?, notes_a=?, notes_b=? WHERE id=?",
        (data.get("content", ""), data.get("notes_a", ""), data.get("notes_b", ""), version_id))
    # Sync updated_at on parent essay
    row = db.execute("SELECT essay_id FROM essay_versions WHERE id=?", (version_id,)).fetchone()
    if row:
        db.execute("UPDATE essays SET updated_at=? WHERE id=?", (now_str(), row["essay_id"]))
    db.commit()
    return jsonify({"ok": True})


# ── AI Analysis ───────────────────────────────────────────

IELTS_CRITERIA = """9分:
- Task Response: fully addresses all parts of the task; presents a fully developed position with relevant, fully extended and well supported ideas
- Coherence & Cohesion: uses cohesion in such a way that it attracts no attention; skilfully manages paragraphing
- Lexical Resource: uses a wide range of vocabulary with very natural and sophisticated control; rare minor errors occur only as 'slips'
- Grammatical Range & Accuracy: uses a wide range of structures with full flexibility and accuracy; rare minor errors occur only as 'slips'

8分:
- Task Response: sufficiently addresses all parts of the task; presents a well-developed response with relevant, extended and supported ideas
- Coherence & Cohesion: sequences information and ideas logically; manages all aspects of cohesion well; uses paragraphing sufficiently and appropriately
- Lexical Resource: uses a wide range of vocabulary fluently and flexibly to convey precise meanings; skilfully uses uncommon lexical items but may be occasional inaccuracies in word choice and collocation; rare errors in spelling and/or word formation
- Grammatical Range & Accuracy: uses a wide range of structures; the majority of sentences are error-free; makes only very occasional errors or inappropriacies

7分:
- Task Response: addresses all parts of the task; presents a clear position throughout; presents, extends and supports main ideas, but may tend to overgeneralise and/or supporting ideas may lack focus
- Coherence & Cohesion: logically organises information and ideas with clear progression throughout; uses a range of cohesive devices appropriately although may be some under-use; presents a clear central topic within each paragraph
- Lexical Resource: uses a sufficient range of vocabulary to allow some flexibility and precision; uses less common lexical items with some awareness of style and collocation; may produce occasional errors in word choice, spelling and/or word formation
- Grammatical Range & Accuracy: uses a variety of complex structures; produces frequent error-free sentences; has good control of grammar and punctuation but may make a few errors

6分:
- Task Response: addresses all parts of the task although some parts may be more fully covered than others; presents a relevant position although conclusions may become unclear or repetitive; presents relevant main ideas but some may be inadequately developed/unclear
- Coherence & Cohesion: arranges information and ideas coherently with clear overall progression; uses cohesive devices effectively, but cohesion within and/or between sentences may be faulty or mechanical; may not always use referencing clearly or appropriately; uses paragraphing, but not always logically
- Lexical Resource: uses an adequate range of vocabulary for the task; attempts to use less common vocabulary but with some inaccuracy; makes some errors in spelling and/or word formation, but they do not impede communication
- Grammatical Range & Accuracy: uses a mix of simple and complex sentence forms; makes some errors in grammar and punctuation but they rarely reduce communication

5分:
- Task Response: addresses the task only partially; expresses a position but development is not always clear and there may be no conclusions drawn; presents some main ideas but limited and not sufficiently developed; may be irrelevant detail
- Coherence & Cohesion: presents information with some organisation but may lack overall progression; makes inadequate, inaccurate or over-use of cohesive devices; may be repetitive; may not write in paragraphs or paragraphing may be inadequate
- Lexical Resource: uses a limited range of vocabulary minimally adequate for the task; may make noticeable errors in spelling and/or word formation that may cause difficulty for the reader
- Grammatical Range & Accuracy: uses only a limited range of structures; attempts complex sentences but these tend to be less accurate than simple sentences; may make frequent grammatical errors; punctuation may be faulty

4分:
- Task Response: addresses the task only in a minimal way or answer is tangential; presents a position but unclear; presents some main ideas but difficult to identify and may be repetitive, irrelevant or not well supported
- Coherence & Cohesion: presents information and ideas but not arranged coherently and no clear progression; uses some basic cohesive devices but may be inaccurate or repetitive; may not write in paragraphs or use may be confusing
- Lexical Resource: uses only basic vocabulary which may be used repetitively or inappropriate for the task; has limited control of word formation and/or spelling; errors may cause strain for the reader
- Grammatical Range & Accuracy: uses only a very limited range of structures with only rare use of subordinate clauses; some structures are accurate but errors predominate, and punctuation is often faulty

3分:
- Task Response: does not adequately address any part of the task; does not express a clear position; presents few ideas largely undeveloped or irrelevant
- Coherence & Cohesion: does not organise ideas logically; may use a very limited range of cohesive devices without demonstrating logical relationship between ideas
- Lexical Resource: uses only a very limited range of words and expressions with very limited control of word formation and/or spelling; errors may severely distort the message
- Grammatical Range & Accuracy: attempts sentence forms but errors in grammar and punctuation predominate and distort meaning

2分: barely responds to the task; very little control of organisational features; extremely limited vocabulary; cannot use sentence forms except in memorised phrases
1分: completely fails to address the task; fails to communicate any message; can only use a few isolated words; cannot use sentence forms at all"""


@app.route("/api/versions/<version_id>/analyze", methods=["POST"])
def analyze_essay(version_id):
    db = get_db()
    row = db.execute(
        """SELECT v.*, e.type, e.number, e.question_text, e.question_image, e.model_essay
           FROM essay_versions v JOIN essays e ON v.essay_id = e.id
           WHERE v.id=?""", (version_id,)).fetchone()
    if not row:
        return jsonify({"error": "Version not found"}), 404

    row = dict(row)
    essay_type = "Task 1 (小作文/图表题)" if row["type"] == "task1" else "Task 2 (大作文/议论文)"

    question_block = row["question_text"] or "(无题目文字)"
    model_block = row["model_essay"] or "(未提供参考范文)"

    has_model = bool(row["model_essay"] and row["model_essay"].strip())
    model_calibration = ""
    if has_model:
        model_calibration = f"""
【评分校准要求（极其重要）】
你同时看到了参考范文和学生作文。请你先给参考范文打分（范文通常处于 7.0-8.0 水平，具体取决于其实际质量），然后以范文的分数为基准，为学生作文打分。
- 学生作文的分数必须严格低于范文 0.5-2.0 分（除非学生作文确实与范文水平相当）
- 中国大陆雅思考生写作平均分约为 5.5-5.8。请不要给出虚高的分数。5.5-6.0 是大多数考生的正常水平，6.5 已属较好，7.0+ 仅限高质量作文
- 如果学生作文有明显的模板痕迹、背诵套句、中式英语，请在 TR 或 LR 维度从严扣分"""

    prompt = f"""你是一位资深雅思写作考官，拥有 15 年阅卷经验。请严格按照以下雅思官方评分标准，对这篇{essay_type}进行严格批改。

【核心原则】
1. 严禁给分虚高。中国大陆考生写作平均分仅 5.6。6.0 已是合格水平，6.5 属中上，7.0+ 仅限优秀作文。
2. 评分必须逐条对照官方标准，每个分数都要在原文中找到扣分依据。
3. 宁可严格，不可松泛。不确定时取低不取高。
{model_calibration}

【雅思官方评分标准】
{IELTS_CRITERIA}

【作文题目】
{question_block}

【学生作文】
{row["content"]}

【参考范文】
{model_block}

【分析要求】
请严格按照以下 JSON 格式返回结果（不要包含任何 markdown 标记，只返回纯 JSON）：

{{
  "scores": {{
    "overall": 5.5,
    "tr": 5.5,
    "cc": 5.5,
    "lr": 5.5,
    "gra": 5.5
  }},
  "summary": "一句话总评，直接点出最致命的 1-2 个问题。语气直白不客气。",
  "weaknesses": {{
    "severe": [
      {{"dimension": "TR", "description": "具体问题", "quote": "原文句子（必须逐字引用）", "deduction_reason": "对应官方标准第 X 档的描述，解释为何扣分"}}
    ],
    "moderate": [],
    "minor": []
  }},
  "revisions": [
    {{
      "original": "原文句子或段落（必须逐字引用）",
      "diagnosis": "为什么这里有问题",
      "revised": "修改后的完整句子/段落",
      "explanation": "改了什么、为什么要这样改",
      "improvement": ["TR"]
    }}
  ],
  "model_comparison": {{
    "structure": "",
    "ideas": "",
    "vocabulary": "",
    "sentences": ""
  }}
}}

严格要求：
- 评分必须基于官方标准原文措辞，不得自己发挥。每个分数对应标准中的关键描述词必须与原文匹配
- severe 是严重问题（直接拉低 0.5-1 分的问题），moderate 是明显不足，minor 是细微瑕疵。不要为了凑数添加不存在的 minor 问题
- 每个 weakness 的 quote 必须是作文中真实存在的原句，不得编造或概括
- revisions 至少给出 3 条具体修改方案，必须覆盖不同的维度
- 如有参考范文：model_comparison 四个字段填写具体对比（逐项说明学生与范文的差距）；如无范文：四个字段均填"未提供范文"
- 只返回 JSON，不要有任何额外文字"""

    try:
        req = Request(DEEPSEEK_URL, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {DEEPSEEK_KEY}")
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是一位资深雅思写作考官。你只返回 JSON，不返回任何其他内容。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 8192
        }).encode("utf-8")
        req.data = body
        resp = urlopen(req, timeout=120)
        result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]

        # Strip markdown code fences if present
        content = re.sub(r'^```(?:json)?\s*\n?', '', content.strip())
        content = re.sub(r'\n?```\s*$', '', content)

        analysis = json.loads(content)

        # Validate & store
        scores = analysis.get("scores", {})
        w_json = json.dumps(analysis.get("weaknesses", {}), ensure_ascii=False)
        r_json = json.dumps(analysis.get("revisions", []), ensure_ascii=False)
        m_json = json.dumps(analysis.get("model_comparison", {}), ensure_ascii=False)

        db.execute("DELETE FROM analyses WHERE version_id=?", (version_id,))
        db.execute(
            """INSERT INTO analyses (id, version_id, overall, tr, cc, lr, gra, summary,
               weaknesses_json, revisions_json, model_comparison_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4())[:8], version_id,
             scores.get("overall"), scores.get("tr"), scores.get("cc"),
             scores.get("lr"), scores.get("gra"),
             analysis.get("summary", ""),
             w_json, r_json, m_json))
        db.execute("UPDATE essays SET updated_at=? WHERE id=(SELECT essay_id FROM essay_versions WHERE id=?)",
                   (now_str(), version_id))
        db.commit()
        return jsonify(analysis)

    except (URLError, json.JSONDecodeError, KeyError) as e:
        return jsonify({"error": f"AI 批改失败: {str(e)}"}), 500


# ── Annotations ───────────────────────────────────────────

@app.route("/api/versions/<version_id>/annotations", methods=["GET"])
def list_annotations(version_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM annotations WHERE version_id=? ORDER BY created_at",
        (version_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/versions/<version_id>/annotations", methods=["POST"])
def create_annotation(version_id):
    data = request.get_json()
    ann_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        "INSERT INTO annotations (id, version_id, selected_text, note) VALUES (?,?,?,?)",
        (ann_id, version_id, data.get("selected_text", ""), data.get("note", "")))
    # update essay updated_at
    row = db.execute("SELECT essay_id FROM essay_versions WHERE id=?", (version_id,)).fetchone()
    if row:
        db.execute("UPDATE essays SET updated_at=? WHERE id=?", (now_str(), row["essay_id"]))
    db.commit()
    return jsonify({"id": ann_id}), 201


@app.route("/api/annotations/<ann_id>", methods=["PUT"])
def update_annotation(ann_id):
    data = request.get_json()
    db = get_db()
    db.execute("UPDATE annotations SET selected_text=?, note=? WHERE id=?",
               (data.get("selected_text", ""), data.get("note", ""), ann_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/annotations/<ann_id>", methods=["DELETE"])
def delete_annotation(ann_id):
    db = get_db()
    db.execute("DELETE FROM annotations WHERE id=?", (ann_id,))
    db.commit()
    return jsonify({"ok": True})


# ── Model Essay Annotations ────────────────────────────────

@app.route("/api/essays/<essay_id>/model-annotations", methods=["GET"])
def list_model_annotations(essay_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM model_annotations WHERE essay_id=? ORDER BY created_at",
        (essay_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/essays/<essay_id>/model-annotations", methods=["POST"])
def create_model_annotation(essay_id):
    data = request.get_json()
    ann_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        "INSERT INTO model_annotations (id, essay_id, selected_text, note) VALUES (?,?,?,?)",
        (ann_id, essay_id, data.get("selected_text", ""), data.get("note", "")))
    db.execute("UPDATE essays SET updated_at=? WHERE id=?", (now_str(), essay_id))
    db.commit()
    return jsonify({"id": ann_id}), 201


@app.route("/api/model-annotations/<ann_id>", methods=["DELETE"])
def delete_model_annotation(ann_id):
    db = get_db()
    db.execute("DELETE FROM model_annotations WHERE id=?", (ann_id,))
    db.commit()
    return jsonify({"ok": True})


# ── Startup ───────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"Database: {DB_PATH}")
    app.run(host="127.0.0.1", port=5020, debug=True)
