"""
NEWS SCANNER & SENTIMENT ANALYZER
===================================
Scans free financial news RSS feeds and scores each stock's
sentiment using keyword analysis. Returns a sentiment score
per ticker that feeds into the News Sentiment strategy.

No API keys needed — uses public RSS feeds.

Optional FinBERT upgrade: set USE_FINBERT = True in config.py and
install `transformers` + `torch` to replace the keyword scorer with
local neural-network inference (ProsusAI/finbert, ~400 MB, cached).
"""

import re
import logging

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from config import USE_FINBERT, FINBERT_MAX_HEADLINES
except ImportError:
    USE_FINBERT = False
    FINBERT_MAX_HEADLINES = 20

logger = logging.getLogger("TradingBot")

# ============================================================
# SENTIMENT WORD LISTS
# ============================================================

POSITIVE_WORDS = {
    "surge", "surges", "surging", "soar", "soars", "soaring",
    "rally", "rallies", "rallying", "jump", "jumps", "jumping",
    "gain", "gains", "gaining", "rise", "rises", "rising",
    "boost", "boosts", "boosting", "upgrade", "upgrades",
    "beat", "beats", "beating", "outperform", "outperforms",
    "profit", "profits", "profitable", "revenue", "growth",
    "strong", "stronger", "strongest", "bullish", "bull",
    "record", "high", "highs", "buy", "overweight",
    "positive", "optimistic", "upbeat", "recovery", "recover",
    "breakthrough", "innovation", "deal", "partnership",
    "acquisition", "expand", "expansion", "dividend",
    "exceed", "exceeds", "exceeded", "expectations",
    "momentum", "breakout", "opportunity", "approve", "approved",
}

NEGATIVE_WORDS = {
    "crash", "crashes", "crashing", "plunge", "plunges", "plunging",
    "drop", "drops", "dropping", "fall", "falls", "falling",
    "decline", "declines", "declining", "sink", "sinks", "sinking",
    "loss", "losses", "losing", "miss", "misses", "missed",
    "downgrade", "downgrades", "sell", "underperform",
    "weak", "weaker", "weakest", "bearish", "bear",
    "low", "lows", "warning", "warns", "layoff", "layoffs",
    "cut", "cuts", "cutting", "debt", "default", "bankruptcy",
    "investigate", "investigation", "lawsuit", "fraud",
    "recall", "penalty", "fine", "fined", "scandal",
    "underweight", "negative", "pessimistic", "risk", "risky",
    "volatile", "volatility", "concern", "worried", "fear",
}

# Map keywords in news to standard tickers
TICKER_KEYWORDS = {
    "apple": "AAPL", "aapl": "AAPL",
    "microsoft": "MSFT", "msft": "MSFT",
    "google": "GOOGL", "alphabet": "GOOGL", "googl": "GOOGL",
    "amazon": "AMZN", "amzn": "AMZN",
    "nvidia": "NVDA", "nvda": "NVDA",
    "meta": "META", "facebook": "META",
    "tesla": "TSLA", "tsla": "TSLA",
    "netflix": "NFLX", "nflx": "NFLX",
    "amd": "AMD", "intel": "INTC", "intc": "INTC",
    "disney": "DIS", "dis": "DIS",
    "nike": "NKE", "coca-cola": "KO", "ko": "KO",
    "walmart": "WMT", "wmt": "WMT",
    "jpmorgan": "JPM", "jpm": "JPM",
    "visa": "V", "mastercard": "MA",
    "paypal": "PYPL", "uber": "UBER",
    "airbnb": "ABNB", "spotify": "SPOT",
    "salesforce": "CRM", "adobe": "ADBE",
    "oracle": "ORCL", "ibm": "IBM",
    "boeing": "BA", "ford": "F",
    "starbucks": "SBUX", "mcdonald": "MCD",
    "pfizer": "PFE", "johnson": "JNJ",
    "costco": "COST", "broadcom": "AVGO",
    "qualcomm": "QCOM", "pep": "PEP", "pepsi": "PEP",
}

NEWS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META&region=US&lang=en-US",
    "https://news.google.com/rss/search?q=stock+market+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=stocks+earnings+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?region=US&lang=en-US",
]


# ============================================================
# FINBERT SENTIMENT ANALYSER
# ============================================================

class FinBERTSentimentAnalyser:
    """
    Local FinBERT inference using HuggingFace transformers.
    Downloads ProsusAI/finbert on first use (~400 MB, cached after).

    Produces sentiment scores in [-1, +1]:
      positive label  →  +score
      negative label  →  -score
      neutral  label  →   0

    Falls back to keyword scoring if transformers is not installed.
    """

    MODEL_NAME = "ProsusAI/finbert"

    def __init__(self):
        self._pipeline = None
        self._available = False
        self._load()

    def _load(self):
        try:
            from transformers import pipeline
            logger.info(
                "Loading FinBERT model (first run will download ~400 MB)..."
            )
            self._pipeline = pipeline(
                "text-classification",
                model=self.MODEL_NAME,
                tokenizer=self.MODEL_NAME,
                return_all_scores=True,
                device=-1,  # CPU inference
            )
            self._available = True
            logger.info("FinBERT loaded successfully.")
        except ImportError:
            logger.warning(
                "transformers not installed — FinBERT unavailable. "
                "Run: pip install transformers torch"
            )
        except Exception as exc:
            logger.warning(f"FinBERT load failed: {exc}")

    def score(self, texts: list) -> float:
        """
        Score a list of headline strings. Returns aggregate score in [-1, +1].
        """
        if not self._available or not texts:
            return 0.0
        try:
            scores = []
            for text in texts[:FINBERT_MAX_HEADLINES]:
                text = text[:512]  # FinBERT max token limit
                result = self._pipeline(text)[0]
                label_scores = {r["label"].lower(): r["score"] for r in result}
                # positive=+1, negative=-1, neutral=0
                score = (
                    label_scores.get("positive", 0)
                    - label_scores.get("negative", 0)
                )
                scores.append(score)
            return float(sum(scores) / len(scores)) if scores else 0.0
        except Exception as exc:
            logger.warning(f"FinBERT inference error: {exc}")
            return 0.0


# Module-level singleton — initialised once per process, not per call.
# Only created when USE_FINBERT is True so that import cost is zero
# when the feature is disabled.
_finbert_analyser: FinBERTSentimentAnalyser | None = None

if USE_FINBERT:
    try:
        _finbert_analyser = FinBERTSentimentAnalyser()
    except Exception as _finbert_init_err:
        logger.warning(f"FinBERT singleton init failed: {_finbert_init_err}")


# ============================================================
# NEWS SCANNER
# ============================================================

class NewsScanner:
    """Scans financial news and returns sentiment per stock ticker."""

    def __init__(self):
        if feedparser is None:
            logger.warning("feedparser not installed — news scanning disabled")

    def get_sentiment_scores(self, stock_universe, exclude_symbols=None):
        """
        Scan news and return sentiment scores for stocks.

        When USE_FINBERT=True and FinBERT is loaded, uses neural-network
        inference on headlines instead of the keyword scorer.

        Args:
            stock_universe:   list of ticker strings to look for
            exclude_symbols:  set of symbols to skip

        Returns:
            dict of {symbol: sentiment_score} — scores in [-1.0, +1.0]
        """
        if feedparser is None:
            return {}
        if exclude_symbols is None:
            exclude_symbols = set()

        articles = self._fetch_articles()
        if not articles:
            return {}

        # ---- FinBERT path ----
        if (USE_FINBERT
                and _finbert_analyser is not None
                and _finbert_analyser._available):
            ticker_headlines: dict = {}
            for article in articles:
                text = f"{article['title']} {article['summary']}"
                headline = article["title"][:512]
                for ticker in self._find_tickers(text):
                    if ticker not in stock_universe or ticker in exclude_symbols:
                        continue
                    ticker_headlines.setdefault(ticker, []).append(headline)

            result = {}
            for ticker, headlines in ticker_headlines.items():
                result[ticker] = round(
                    _finbert_analyser.score(headlines[:FINBERT_MAX_HEADLINES]), 3
                )
            logger.info(
                f"FinBERT news scan: {len(articles)} articles, "
                f"{len(result)} stocks scored"
            )
            return result

        # ---- Keyword fallback path ----
        ticker_scores: dict = {}
        for article in articles:
            text = f"{article['title']} {article['summary']}"
            sentiment = self._score_text(text)
            for ticker in self._find_tickers(text):
                if ticker not in stock_universe or ticker in exclude_symbols:
                    continue
                ticker_scores.setdefault(ticker, []).append(sentiment)

        result = {}
        for ticker, scores in ticker_scores.items():
            avg = sum(scores) / len(scores) if scores else 0
            result[ticker] = round(avg, 3)

        logger.info(
            f"News scan: {len(articles)} articles, "
            f"{len(result)} stocks with sentiment"
        )
        return result

    def _fetch_articles(self):
        """Fetch articles from RSS feeds."""
        articles = []
        for url in NEWS_FEEDS:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:20]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", entry.get("description", "")),
                    })
            except Exception as exc:
                logger.warning(f"Feed error: {exc}")
        return articles

    def _score_text(self, text):
        """Calculate sentiment from -1 to +1."""
        words = re.findall(r'\b[a-z]+\b', text.lower())
        if not words:
            return 0.0
        pos = sum(1 for w in words if w in POSITIVE_WORDS)
        neg = sum(1 for w in words if w in NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    def _find_tickers(self, text):
        """Find stock tickers mentioned in text."""
        text_lower = text.lower()
        found = set()
        for keyword, ticker in TICKER_KEYWORDS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', text_lower):
                found.add(ticker)
        return found
