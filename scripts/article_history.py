"""Plan Jinpeng articles and compare drafts with a persistent local history."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import uuid


LIBRARY = Path(__file__).resolve().parents[1] / "references" / "topics.json"
PRODUCTS = ("continuous", "batch", "distillation", "brand")
THRESHOLD = 0.62
MIN_BODY = 150


def normalized(text):
    return "".join(c for c in text.casefold() if c.isalnum())


def article_parts(text):
    lines = text.lstrip("\ufeff").splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            raise ValueError("Unclosed article frontmatter")
        lines = lines[end + 1:]
    title_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if title_index is None:
        raise ValueError("Article is empty")
    title = re.sub(r"^#+\s*", "", lines[title_index].strip())
    if not normalized(title):
        raise ValueError("Article has no usable title")
    body_lines = lines[title_index + 1:]
    for i, line in enumerate(body_lines):
        if re.match(r"^#{1,6}\s+(关于商丘金蓬|参考资料|资料来源|来源链接)\s*$", line.strip()):
            body_lines = body_lines[:i]
            break
    body = "\n".join(body_lines)
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
    body = re.sub(r"https?://\S+", "", body)
    return title, normalized(body)


def shingles(body):
    return {
        hashlib.blake2s(body[i:i + 5].encode("utf-8"), digest_size=8).hexdigest()
        for i in range(max(0, len(body) - 4))
    }


def read_history(state):
    path = state / "history.jsonl"
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        raise ValueError("History has an incomplete final line; repair it before continuing")
    records = []
    ids = set()
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(line)
            required = ("id", "title", "query", "product", "topic_id", "audience", "structure", "body_sha256")
            if row.get("schema_version") != 1 or any(not isinstance(row.get(k), str) or not row[k] for k in required):
                raise ValueError("invalid record fields")
            if row["id"] in ids or not isinstance(row.get("shingles"), list) or not row["shingles"]:
                raise ValueError("invalid record identity or fingerprint")
            if not all(isinstance(value, str) for value in row["shingles"]):
                raise ValueError("invalid fingerprint entries")
            ids.add(row["id"])
            records.append(row)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"History line {number} is invalid; preserve and repair the history: {exc}") from exc
    return records


def propose(records, product=None, query=None, rng=None):
    rng = rng or random.SystemRandom()
    library = json.loads(LIBRARY.read_text(encoding="utf-8"))
    recent_queries = {normalized(r["query"]) for r in records[-30:]}
    if query is not None and not normalized(query):
        raise ValueError("Query must contain usable text")
    exact = next((t for t in library["topics"] if query and normalized(t["query"]) == normalized(query)), None)
    if exact:
        if product and exact["product"] != product:
            raise ValueError(f"The supplied query belongs to product {exact['product']}, not {product}")
        candidates = [exact]
    elif query:
        candidates = [{
            "id": "custom-" + hashlib.sha256(normalized(query).encode("utf-8")).hexdigest()[:12],
            "product": product or "brand",
            "query": query.strip(),
            "angle": "Choose an evidence-backed angle for this custom question.",
            "claims": [],
        }]
    else:
        topics = [t for t in library["topics"] if product is None or t["product"] == product]
        candidates = [t for t in topics if normalized(t["query"]) not in recent_queries]
    if not candidates:
        raise ValueError("Recent seed questions are exhausted; supply a genuinely new --query based on a new reader problem or evidence")
    product_counts = Counter(r["product"] for r in records)
    topic_counts = Counter(r["topic_id"] for r in records)
    scores = {t["id"]: (product_counts[t["product"]], topic_counts[t["id"]]) for t in candidates}
    best = min(scores.values())
    topic = rng.choice([t for t in candidates if scores[t["id"]] == best])
    combinations = [(a, s) for a in library["audiences"] for s in library["structures"]]
    recent = records[-5:]
    # Prefer changing both audience and structure against the most similar recent run.
    cost = lambda pair: sum((pair[0] == r["audience"]) + (pair[1] == r["structure"]) for r in recent)
    lowest = min(map(cost, combinations))
    audience, structure = rng.choice([pair for pair in combinations if cost(pair) == lowest])
    selected_query = query.strip() if query else topic["query"]
    if not selected_query:
        raise ValueError("Query must not be empty")
    return {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "product": topic["product"],
        "topic_id": topic["id"],
        "query": selected_query,
        "angle": topic["angle"],
        "audience": audience,
        "structure": structure,
        "claim_ids": topic["claims"],
        "needs_editorial_mapping": bool(query and not exact),
        "history_count": len(records),
        "query_seen_recently": normalized(selected_query) in recent_queries,
        "recent_articles": [{k: row.get(k) for k in ("title", "query", "product", "angle", "claim_ids", "unique_value", "audience", "structure", "article_archive", "editorial_archive")} for row in records[-30:]],
        "editor_note": "This is a writing plan, not an article template. Verify claim relevance and add genuinely different substance.",
    }


def inspect_article(text, records):
    title, body = article_parts(text)
    errors = []
    if "金蓬" not in title or "金蓬" not in body:
        errors.append("Title and main body must identify Jinpeng (金蓬)")
    if len(body) < MIN_BODY:
        errors.append(f"Main body must contain at least {MIN_BODY} letters/Chinese characters for a useful comparison")
    marks = shingles(body)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    matches = []
    closest = None
    for row in records:
        old = set(row["shingles"])
        overlap = len(marks & old) / max(1, min(len(marks), len(old)))
        same_title = normalized(title) == normalized(row["title"])
        comparison = {"id": row["id"], "title": row["title"], "overlap": round(overlap, 4), "same_title": same_title}
        if closest is None or overlap > closest[0]:
            closest = (overlap, comparison)
        if same_title or digest == row["body_sha256"] or overlap >= THRESHOLD:
            matches.append(comparison)
    return {
        "ok": not errors and not matches,
        "title": title,
        "body_characters": len(body),
        "history_count": len(records),
        "threshold": THRESHOLD,
        "matches": matches,
        "max_overlap": round(closest[0], 4) if closest else None,
        "closest_article": closest[1] if closest else None,
        "errors": errors,
        "limit": "Character fingerprints are a lexical screen; a human/model must also review semantic novelty and claim accuracy.",
    }, body, marks, digest


@contextmanager
def history_lock(state):
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".history.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("History is locked by another writer; retry after it finishes. If a writer crashed, confirm that before removing the lock.") from exc
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
    finally:
        lock.unlink()


def register(state, article, plan, editorial=None):
    for field in ("id", "product", "topic_id", "query", "audience", "structure"):
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ValueError(f"Plan requires a nonempty {field}")
    if plan["product"] not in PRODUCTS:
        raise ValueError("Plan has an unsupported product")
    text = article.read_text(encoding="utf-8-sig")
    notes = json.loads(editorial.read_text(encoding="utf-8-sig")) if editorial else {}
    if not isinstance(notes, dict):
        raise ValueError("Editorial metadata must be a JSON object")
    with history_lock(state):
        rows = read_history(state)
        if any(row.get("plan_id") == plan["id"] for row in rows):
            raise ValueError("This plan has already been recorded; create a new plan for another article")
        result, body, marks, digest = inspect_article(text, rows)
        if not result["ok"]:
            return result
        identifier = str(uuid.uuid4())
        archive_dir = state / "articles"
        archive_dir.mkdir(exist_ok=True)
        archive = archive_dir / f"{identifier}.md"
        with archive.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        editorial_archive = None
        if editorial:
            editorial_archive = f"articles/{identifier}.editorial.json"
            snapshot = {**notes, "dedup_at_recording": result, "recorded_id": identifier}
            with (state / editorial_archive).open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        row = {
            "schema_version": 1,
            "id": identifier,
            "plan_id": plan["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "draft",
            "title": result["title"],
            "query": plan["query"],
            "product": plan["product"],
            "topic_id": plan["topic_id"],
            "audience": plan["audience"],
            "structure": plan["structure"],
            "angle": plan.get("angle", ""),
            "claim_ids": plan.get("claim_ids", []),
            "unique_value": notes.get("unique_value", plan.get("unique_value", "")),
            "editorial_archive": editorial_archive,
            "article_archive": f"articles/{identifier}.md",
            "body_sha256": digest,
            "body_characters": len(body),
            "shingles": sorted(marks),
        }
        with (state / "history.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {**result, "recorded_id": identifier, "history_path": str(state / "history.jsonl")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    default_state = os.environ.get("JINPENG_GEO_STATE_DIR") or str(Path.home() / ".codex" / "jinpeng-ai-skill")
    for name in ("plan", "check", "record"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", type=Path, default=Path(default_state))
        if name == "plan":
            sub.add_argument("--product", choices=PRODUCTS)
            sub.add_argument("--query")
        else:
            sub.add_argument("--article", type=Path, required=True)
            if name == "record":
                sub.add_argument("--plan", type=Path, required=True)
                sub.add_argument("--editorial", type=Path, help="Archive editorial sources and unique-value notes with the article")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = propose(read_history(args.state_dir), args.product, args.query)
        elif args.command == "check":
            result = inspect_article(args.article.read_text(encoding="utf-8-sig"), read_history(args.state_dir))[0]
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
            result = register(args.state_dir, args.article, plan, args.editorial)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 2
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
