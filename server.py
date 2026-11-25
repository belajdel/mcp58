"""
MCP server for Loi organique n° 2017-58 du 11 août 2017
(élimination de la violence à l'égard des femmes - Tunisie).

Run (stdio, for LM Studio / Claude Desktop / Cursor):
    python server.py

The JSON file is looked up in this order:
    1. env var LAW_JSON_PATH
    2. ./loi-organique-2017-58.json next to this script
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

JSON_PATH = Path(
    os.environ.get(
        "LAW_JSON_PATH",
        Path(__file__).parent / "loi-organique-2017-58.json",
    )
)

with open(JSON_PATH, encoding="utf-8") as f:
    LAW: dict[str, Any] = json.load(f)

ARTICLES: dict[int, dict[str, Any]] = {a["number"]: a for a in LAW["articles"]}
STRUCTURE: list[dict[str, Any]] = LAW["structure"]

# chapter_id / section_id -> metadata
CHAPTERS: dict[str, dict[str, Any]] = {c["id"]: c for c in STRUCTURE}
SECTIONS: dict[str, dict[str, Any]] = {
    s["id"]: {**s, "chapter_id": c["id"]}
    for c in STRUCTURE
    for s in c.get("sections", [])
}

# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def fold(text: str) -> str:
    """Lowercase + strip accents, keeping string length identical so offsets
    in the folded text map 1:1 to the original (used for snippets)."""
    out = []
    for ch in text:
        if ch in "’‘`´":
            out.append("'")
            continue
        decomposed = unicodedata.normalize("NFD", ch)
        base = decomposed[0] if decomposed else ch
        out.append(base.lower())
    return "".join(out)


STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou", "en", "au",
    "aux", "a", "l", "d", "est", "sont", "que", "qui", "par", "pour", "sur",
    "dans", "ce", "se", "sa", "son", "ses", "the", "of", "and",
}


def tokenize(query: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", fold(query))
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def _as_list(value: Any) -> list[str]:
    return list(value) if isinstance(value, list) else []


def render_block(obj: dict[str, Any]) -> str:
    """Render the body of an article or an amendment as plain text."""
    parts: list[str] = []
    if obj.get("text"):
        parts.append(obj["text"])
    parts.extend(_as_list(obj.get("paragraphs")))
    for item in _as_list(obj.get("items")):
        parts.append(f"- {item}")
    for d in obj.get("definitions", []) or []:
        line = f"- {d['term']} : {d['definition']}"
        if d.get("note"):
            line += f" ({d['note']})"
        parts.append(line)
    parts.extend(_as_list(obj.get("additional_provisions")))
    return "\n".join(parts)


def render_article(article: dict[str, Any]) -> str:
    """Full plain-text rendering of an article, including amendments."""
    lines = [article["label"], render_block(article)]
    for am in article.get("amendments", []) or []:
        lines.append("")
        lines.append(f"[{am['label']}]")
        lines.append(render_block(am))
    return "\n".join(l for l in lines if l is not None).strip()


def search_blob(article: dict[str, Any]) -> str:
    """Everything searchable about an article, as one string."""
    chunks = [article["label"], render_block(article)]
    for p in article.get("penalties", []) or []:
        chunks.append(f"{p['condition']} {p['penalty']}")
    for am in article.get("amendments", []) or []:
        chunks.append(f"Code pénal article {am['article']} {am['label']}")
        chunks.append(render_block(am))
        for p in am.get("penalties", []) or []:
            chunks.append(f"{p['condition']} {p['penalty']}")
    return "\n".join(chunks)


BLOBS = {n: search_blob(a) for n, a in ARTICLES.items()}
FOLDED_BLOBS = {n: fold(b) for n, b in BLOBS.items()}


def make_snippet(n: int, tokens: list[str], width: int = 220) -> str:
    folded, original = FOLDED_BLOBS[n], BLOBS[n]
    pos = -1
    for t in tokens:
        pos = folded.find(t)
        if pos != -1:
            break
    if pos == -1:
        return original[:width].replace("\n", " ")
    start = max(0, pos - width // 3)
    end = min(len(original), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(original) else ""
    return prefix + original[start:end].replace("\n", " ") + suffix


def summarize(article: dict[str, Any]) -> dict[str, Any]:
    ch = CHAPTERS.get(article["chapter_id"], {})
    sec = SECTIONS.get(article.get("section_id") or "", {})
    return {
        "number": article["number"],
        "label": article["label"],
        "chapter": f"{ch.get('label', '')} – {ch.get('title', '')}".strip(" –"),
        "section": f"{sec.get('label', '')} – {sec.get('title', '')}".strip(" –")
        if sec
        else None,
    }


def _bad_article(number: int) -> dict[str, Any]:
    return {
        "error": f"Article {number} introuvable. La loi comporte les articles 1 à {max(ARTICLES)}."
    }


# --------------------------------------------------------------------------
# Tool implementations (plain functions, registered with FastMCP below)
# --------------------------------------------------------------------------


def get_law_info() -> dict[str, Any]:
    """Métadonnées de la loi : numéro, date, titre, entrée en vigueur, préambule,
    et nombre d'articles. À appeler en premier pour situer le texte."""
    meta = dict(LAW["law"])
    meta["article_count"] = len(ARTICLES)
    meta["closing"] = LAW.get("closing")
    return meta


def get_structure() -> list[dict[str, Any]]:
    """Plan de la loi : chapitres, sections et numéros d'articles de chacun.
    Les identifiants (ch1, ch2-s1, ...) servent à list_articles()."""
    return STRUCTURE


def get_article(number: int, include_text: bool = True) -> dict[str, Any]:
    """Retourne un article par son numéro (1 à 44), avec sa structure complète
    (items, paragraphes, peines, amendements au Code pénal pour les art. 15 et 16)
    et, si include_text est vrai, un champ 'full_text' prêt à citer.

    Args:
        number: numéro de l'article de la loi n° 2017-58.
        include_text: ajouter le rendu texte complet de l'article.
    """
    article = ARTICLES.get(number)
    if not article:
        return _bad_article(number)
    result = dict(article)
    if include_text:
        result["full_text"] = render_article(article)
    return result


def get_articles(numbers: list[int]) -> list[dict[str, Any]]:
    """Retourne plusieurs articles à la fois (texte complet).

    Args:
        numbers: liste de numéros d'articles, ex. [14, 22, 26].
    """
    out = []
    for n in numbers:
        a = ARTICLES.get(n)
        out.append(
            {**summarize(a), "full_text": render_article(a)} if a else _bad_article(n)
        )
    return out


def list_articles(
    chapter_id: Optional[str] = None, section_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """Liste les articles (numéro, intitulé, chapitre, section), éventuellement
    filtrés par chapitre (ch1..ch5) ou section (ch2-s1, ch2-s2, ch4-s1, ch4-s2, ch4-s3).

    Args:
        chapter_id: identifiant de chapitre, ex. 'ch3'.
        section_id: identifiant de section, ex. 'ch4-s2'.
    """
    if section_id and section_id not in SECTIONS:
        return [{"error": f"Section inconnue : {section_id}", "valid": sorted(SECTIONS)}]
    if chapter_id and chapter_id not in CHAPTERS:
        return [{"error": f"Chapitre inconnu : {chapter_id}", "valid": sorted(CHAPTERS)}]
    out = []
    for a in ARTICLES.values():
        if chapter_id and a["chapter_id"] != chapter_id:
            continue
        if section_id and a.get("section_id") != section_id:
            continue
        out.append(summarize(a))
    return out


def search_articles(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Recherche plein texte (insensible aux accents et à la casse) dans toute la
    loi, y compris les amendements du Code pénal et les peines. Retourne les
    articles les plus pertinents avec un extrait.

    Args:
        query: mots-clés, ex. 'harcèlement sexuel', 'ordonnance de protection durée'.
        limit: nombre maximum de résultats (défaut 5, max 20).
    """
    tokens = tokenize(query)
    if not tokens:
        return []
    limit = max(1, min(limit, 20))
    phrase = " ".join(re.findall(r"[a-z0-9]+", fold(query)))
    scored: list[tuple[float, int]] = []
    for n, folded in FOLDED_BLOBS.items():
        label = fold(ARTICLES[n]["label"])
        score = 0.0
        matched = 0
        for t in tokens:
            c = folded.count(t)
            if c:
                matched += 1
                score += 1 + min(c, 5) * 0.5
        if not matched:
            continue
        score += matched / len(tokens) * 3  # reward covering more query terms
        if phrase and len(tokens) > 1 and phrase in folded:
            score += 5  # exact phrase bonus
        if any(t in label for t in tokens):
            score += 1
        scored.append((score, n))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [
        {
            **summarize(ARTICLES[n]),
            "score": round(score, 2),
            "snippet": make_snippet(n, tokens),
        }
        for score, n in scored[:limit]
    ]


def list_definitions() -> list[str]:
    """Liste les termes définis à l'article 3 (femme, enfant, violence physique,
    morale, sexuelle, politique, économique, discrimination, vulnérabilité, victime)."""
    return [d["term"] for d in ARTICLES[3]["definitions"]]


def get_definition(term: str) -> dict[str, Any]:
    """Retourne la définition légale d'un terme (article 3).

    Args:
        term: terme recherché, ex. 'violence économique', 'victime', 'enfant'.
    """
    needle = fold(term).strip()
    defs = ARTICLES[3]["definitions"]
    exact = [d for d in defs if fold(d["term"]) == needle]
    partial = [d for d in defs if needle and needle in fold(d["term"])]
    reverse = [d for d in defs if needle and fold(d["term"]) in needle]
    matches = exact or partial or reverse
    if not matches:
        return {
            "error": f"Aucune définition trouvée pour « {term} ».",
            "available_terms": [d["term"] for d in defs],
        }
    return {"source": "Article 3", "definitions": matches}


def list_penalties(keyword: Optional[str] = None) -> list[dict[str, Any]]:
    """Liste les sanctions pénales prévues par la loi (peines d'emprisonnement,
    amendes, circonstances aggravantes), y compris celles des dispositions du Code
    pénal modifiées par les articles 15 et 16.

    Args:
        keyword: filtre optionnel, ex. 'viol', 'harcèlement', 'amende', 'emprisonnement à vie'.
    """
    rows: list[dict[str, Any]] = []

    def add(source: str, art_no: int, obj: dict[str, Any]) -> None:
        body = render_block(obj)
        summary = (obj.get("text") or (obj.get("paragraphs") or [""])[0])[:240]
        for p in obj.get("penalties", []) or []:
            rows.append(
                {
                    "source": source,
                    "law_article": art_no,
                    "condition": p["condition"],
                    "penalty": p["penalty"],
                    "offence": summary,
                    "_blob": fold(f"{source} {body} {p['condition']} {p['penalty']}"),
                }
            )

    for n, a in ARTICLES.items():
        add(f"Article {n} de la loi 2017-58", n, a)
        for am in a.get("amendments", []) or []:
            para = f" §{am['paragraph']}" if am.get("paragraph") else ""
            add(f"Code pénal art. {am['article']}{para} (via art. {n})", n, am)

    if keyword:
        toks = tokenize(keyword)
        rows = [
            r
            for r in rows
            if all(re.search(rf"\b{re.escape(t)}\b", r["_blob"]) for t in toks)
        ]  # whole-word match so "viol" does not match "violence"
    for r in rows:
        r.pop("_blob", None)
    return rows


def get_penal_code_provision(article: str, paragraph: Optional[int] = None) -> list[dict[str, Any]]:
    """Retourne les dispositions du Code pénal créées ou modifiées par la loi
    2017-58 (articles 15 et 16), ex. '227' (viol), '226 ter' (harcèlement sexuel),
    '224 bis' (violence conjugale répétée), '208'.

    Args:
        article: numéro d'article du Code pénal, ex. '227 bis'.
        paragraph: numéro de paragraphe optionnel.
    """
    target = re.sub(r"\s+", " ", article.strip().lower())
    results = []
    for a in ARTICLES.values():
        for am in a.get("amendments", []) or []:
            if am["article"].lower() != target:
                continue
            if paragraph is not None and am.get("paragraph") != paragraph:
                continue
            results.append(
                {
                    "law_article": a["number"],
                    **am,
                    "full_text": render_block(am),
                }
            )
    if not results:
        available = sorted(
            {am["article"] for a in ARTICLES.values() for am in a.get("amendments", []) or []}
        )
        return [
            {
                "error": f"Aucune disposition du Code pénal « {article} » dans cette loi.",
                "available_code_penal_articles": available,
            }
        ]
    return results


def get_related_articles(number: int) -> dict[str, Any]:
    """Articles liés à un article donné : ceux qu'il cite, ceux qui le citent,
    et ses voisins dans la même section ou le même chapitre.

    Args:
        number: numéro de l'article.
    """
    a = ARTICLES.get(number)
    if not a:
        return _bad_article(number)

    cites = sorted(a.get("references", []) or [])
    cited_by = sorted(
        n for n, other in ARTICLES.items() if number in (other.get("references") or [])
    )
    group = SECTIONS.get(a.get("section_id") or "", CHAPTERS[a["chapter_id"]])
    siblings = [n for n in group["articles"] if n != number]
    return {
        "article": summarize(a),
        "cites": [summarize(ARTICLES[n]) for n in cites if n in ARTICLES],
        "cited_by": [summarize(ARTICLES[n]) for n in cited_by],
        "same_section_or_chapter": siblings,
    }


def get_victim_rights() -> dict[str, Any]:
    """Synthèse prête à l'emploi des droits de la victime et des mécanismes de
    protection : art. 4 (principes), 13 (droits), 14 (signalement), 26 (mesures de
    la police), 28-29 (audition), 30-38 (ordonnance de protection), 39 (obligations
    des intervenants). Retourne les textes complets."""
    numbers = [4, 13, 14, 26, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39]
    return {
        "articles": [
            {**summarize(ARTICLES[n]), "full_text": render_article(ARTICLES[n])}
            for n in numbers
        ]
    }


# --------------------------------------------------------------------------
# FastMCP wiring
# --------------------------------------------------------------------------

mcp = FastMCP(
    "Loi 2017-58 Tunisie",
    instructions=(
        "Serveur de consultation de la loi organique tunisienne n° 2017-58 du 11 août 2017 "
        "relative à l'élimination de la violence à l'égard des femmes. "
        "Utilise search_articles pour trouver des articles, get_article pour citer le texte exact, "
        "get_definition pour les termes de l'article 3, list_penalties et get_penal_code_provision "
        "pour les sanctions. Cite toujours le numéro d'article. Ces outils donnent le texte de la loi "
        "tel qu'adopté en 2017 : ils ne remplacent pas un avis juridique et ne reflètent pas "
        "d'éventuelles modifications ultérieures."
    ),
)

for _fn in (
    get_law_info,
    get_structure,
    get_article,
    get_articles,
    list_articles,
    search_articles,
    list_definitions,
    get_definition,
    list_penalties,
    get_penal_code_provision,
    get_related_articles,
    get_victim_rights,
):
    mcp.tool(_fn)


@mcp.resource("law://structure")
def structure_resource() -> str:
    """Plan complet de la loi (JSON)."""
    return json.dumps(STRUCTURE, ensure_ascii=False, indent=2)


@mcp.resource("law://article/{number}")
def article_resource(number: int) -> str:
    """Texte complet d'un article."""
    a = ARTICLES.get(int(number))
    return render_article(a) if a else f"Article {number} introuvable."


@mcp.prompt()
def analyse_situation(situation: str) -> str:
    """Analyse une situation à la lumière de la loi 2017-58."""
    return (
        "À l'aide des outils du serveur « Loi 2017-58 Tunisie », analyse la situation suivante : "
        f"\n\n{situation}\n\n"
        "1) Identifie la ou les formes de violence en cause (get_definition). "
        "2) Trouve les infractions et peines applicables (search_articles, list_penalties, "
        "get_penal_code_provision). 3) Indique les droits de la victime et la procédure de protection "
        "(get_victim_rights). Cite chaque article utilisé et rappelle qu'il s'agit d'une information "
        "juridique générale, non d'un avis d'avocat."
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport
