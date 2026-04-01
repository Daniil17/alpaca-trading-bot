"""
NEWS SCANNER & SENTIMENT ANALYZER
===================================
Scans free financial news RSS feeds and scores each stock's
sentiment using keyword analysis. Returns a sentiment score
per ticker that feeds into the News Sentiment strategy.

No API keys needed — uses public RSS feeds.
"""

import re
import logging

try:
    import feedparser
except ImportError:
    feedparser = None

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


class NewsScanner:
    """Scans financial news and returns sentiment per stock ticker."""

    def __init__(self):
        if feedparser is None:
            logger.warning("feedparser not installed — news scanning disabled")

    def get_sentiment_scores(self, stock_universe, exclude_symbols=None):
        """
        Scan news and return sentiment scores for stocks.

        Args:
            stock_universe: list of ticker strings to look for
            exclude_symbols: set of symbols to skip

        Returns:
            dict of {symbol: sentiment_score} for stocks with news
            (sentiment ranges from -1.0 to +1.0)
        """
        if feedparser is None:
            return {}
        if exclude_symbols is None:
            exclude_symbols = set()

        articles = self._fetch_articles()
        if not articles:
            return {}

        # Accumulate sentiment per ticker
        ticker_scores = {}

        for article in articles:
            text = f"{article['title']} {article['summary']}"
            sentiment = self._score_text(text)
            mentioned = self._find_tickers(text)

            for ticker in mentioned:
                if ticker not in stock_universe:
                    continue
                if ticker in exclude_symbols:
                    continue
                if ticker not in ticker_scores:
                    ticker_scores[ticker] = []
                ticker_scores[ticker].append(sentiment)

        # Average the scores
        result = {}
        for ticker, scores in ticker_scores.items():
            avg = sum(scores) / len(scores) if scores else 0
            result[ticker] = round(avg, 3)

        logger.info(f"News scan: {len(articles)} articles, "
                    f"{len(result)} stocks with sentiment")
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
            except Exception as e:
                logger.warning(f"Feed error: {e}")
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
