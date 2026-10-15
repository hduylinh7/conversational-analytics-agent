"""Text normalization and tokenization utilities supporting Vietnamese and English analytics queries."""

import re
import unicodedata

# Vietnamese filler and stop words commonly appearing in conversational analytics questions
VIETNAMESE_STOP_WORDS = {
    "cho",
    "toi",
    "biết",
    "biet",
    "hãy",
    "hay",
    "xin",
    "với",
    "voi",
    "và",
    "va",
    "là",
    "la",
    "theo",
    "của",
    "cua",
    "trong",
    "vào",
    "vao",
    "ở",
    "o",
    "tại",
    "tai",
    "các",
    "cac",
    "những",
    "nhung",
    "bao",
    "nhiêu",
    "nhieu",
    "nào",
    "nao",
    "gì",
    "gi",
}

# English conversational query stop words
ENGLISH_STOP_WORDS = {
    "a",
    "an",
    "the",
    "by",
    "of",
    "in",
    "at",
    "for",
    "to",
    "from",
    "with",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "show",
    "tell",
    "give",
    "get",
    "what",
    "which",
    "how",
    "many",
    "much",
    "across",
    "all",
    "placed",
}

# Common English irregular/regular plural stems
ENGLISH_PLURAL_STEMS = {
    "orders": "order",
    "items": "item",
    "costs": "cost",
    "states": "state",
    "cities": "city",
    "categories": "category",
    "payments": "payment",
    "customers": "customer",
    "sellers": "seller",
    "products": "product",
    "sales": "sale",
    "revenues": "revenue",
    "freights": "freight",
    "fees": "fee",
    "dates": "date",
    "years": "year",
    "months": "month",
}


def strip_vietnamese_diacritics(text: str) -> str:
    """Remove Vietnamese diacritics and convert đ/Đ to d/D.

    Args:
        text: Vietnamese unicode string.

    Returns:
        Accent-free ASCII-like string.
    """
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", stripped)


def normalize_text(text: str) -> str:
    """Normalize text by converting to lowercase, removing punctuation, and collapsing whitespace.

    Args:
        text: Raw input query or metadata phrase.

    Returns:
        Cleaned lowercase string.
    """
    if not text:
        return ""
    # Convert to lowercase
    text = text.lower()
    # Replace punctuation and special characters with spaces, retaining unicode letters and digits
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    # Collapse multiple whitespaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


def stem_token(token: str) -> str:
    """Apply basic domain-specific stemming to English words (plural to singular).

    Args:
        token: Single word token.

    Returns:
        Stemmed word.
    """
    if token in ENGLISH_PLURAL_STEMS:
        return ENGLISH_PLURAL_STEMS[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def tokenize(
    text: str,
    remove_stopwords: bool = False,
    stem: bool = False,
) -> list[str]:
    """Tokenize a string into words with optional stopword filtering and stemming.

    Args:
        text: Text to tokenize.
        remove_stopwords: Whether to filter out common Vietnamese and English stop words.
        stem: Whether to apply plural stemming.

    Returns:
        List of clean word tokens.
    """
    normalized = normalize_text(text)
    if not normalized:
        return []

    tokens = normalized.split()
    if remove_stopwords:
        tokens = [
            t
            for t in tokens
            if t not in VIETNAMESE_STOP_WORDS and t not in ENGLISH_STOP_WORDS
        ]

    if stem:
        tokens = [stem_token(t) for t in tokens]

    return tokens


def extract_ngrams(tokens: list[str], min_n: int = 1, max_n: int = 4) -> list[str]:
    """Generate n-gram phrases from a list of tokens.

    Args:
        tokens: List of word tokens.
        min_n: Minimum n-gram length.
        max_n: Maximum n-gram length.

    Returns:
        List of joined n-gram phrases.
    """
    ngrams: list[str] = []
    n_tokens = len(tokens)
    for n in range(min_n, min(max_n + 1, n_tokens + 1)):
        for i in range(n_tokens - n + 1):
            ngrams.append(" ".join(tokens[i : i + n]))
    return ngrams
