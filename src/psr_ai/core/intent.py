"""Multilingual intent classification for the `sa do` natural language interface.

Classifies free-text instructions (any language) into PSR.ai actions.
Works entirely offline via keyword/pattern matching - no API key required.
When confidence is low, returns a clarifying question in the same language.

Supported actions:
  download, download_profile, screenshot, pdf, crawl, browse, scrape,
  scrape_species, accessibility, codegen, intercept, test_all, form,
  trace, api_get, api_post, replay, record, heal, analyze, publish

Language detection:
  Detects Hebrew, Arabic, Russian, Chinese, Japanese, Korean, Spanish,
  French, German, Italian, Portuguese, Turkish, and English (default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    action: str            # canonical action name
    url: str | None        # extracted URL, if any
    params: dict           # extra parameters (browser, format, selector, query, etc.)
    confidence: float      # 0.0 - 1.0
    raw: str               # original user input
    language: str          # detected language code (en, he, ar, ru, etc.)
    clarification: str | None = None   # question to ask user when confidence < threshold


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _detect_language(text: str) -> str:
    """Detect language from character ranges. Returns BCP-47 language tag."""
    he_count = sum(1 for c in text if "א" <= c <= "ת")  # Hebrew
    ar_count = sum(1 for c in text if "؀" <= c <= "ۿ")  # Arabic
    ru_count = sum(1 for c in text if "Ѐ" <= c <= "ӿ")  # Cyrillic
    zh_count = sum(1 for c in text if "一" <= c <= "鿿")  # CJK Chinese
    ja_count = sum(1 for c in text if "぀" <= c <= "ヿ")  # Japanese kana
    ko_count = sum(1 for c in text if "가" <= c <= "힣")  # Korean

    total = max(len(text), 1)
    scores = {
        "he": he_count / total,
        "ar": ar_count / total,
        "ru": ru_count / total,
        "zh": zh_count / total,
        "ja": ja_count / total,
        "ko": ko_count / total,
    }
    best_lang, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score > 0.08:
        return best_lang

    # Latin-script language heuristics
    text_lower = text.lower()
    if any(w in text_lower for w in ["le ", "la ", "les ", "un ", "une ", "des ", "est ", "pour ", "avec "]):
        return "fr"
    if any(w in text_lower for w in ["der ", "die ", "das ", "ein ", "eine ", "und ", "mit ", "ist "]):
        return "de"
    if any(w in text_lower for w in [" el ", " la ", " los ", " las ", " un ", " una ", " con ", " que "]):
        return "es"
    if any(w in text_lower for w in [" il ", " lo ", " gli ", " una ", " con ", " per ", " che "]):
        return "it"
    if any(w in text_lower for w in [" o ", " a ", " os ", " as ", " um ", " uma ", " com ", " por "]):
        return "pt"
    if any(w in text_lower for w in ["bir ", "bu ", "ile ", "için ", "veya ", "değil "]):
        return "tr"
    return "en"


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://[^\sא-ת\"']+|"
    r"(?<!\w)(?:www\.)[^\sא-ת\"']+\.[a-z]{2,}[^\sא-ת\"']*",
    re.IGNORECASE,
)


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    if m:
        url = m.group(0).rstrip(".,;:)")
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


# ---------------------------------------------------------------------------
# Keyword maps per language
# ---------------------------------------------------------------------------

# Each entry: (action, keywords_list, base_confidence)
# Keywords are matched case-insensitively against the normalized input.
_RULES: list[tuple[str, list[str], float]] = [
    # Download / video
    ("download", [
        # English
        "download", "save video", "get video", "fetch video", "grab video",
        "save audio", "rip audio", "get audio",
        # Hebrew
        "תוריד", "הורד", "תשמור", "שמור סרטון", "תביא סרטון",
        "הורדה", "הורד לי", "תוריד לי",
        # Arabic
        "تحميل", "حمل", "نزل",
        # Russian
        "скачать", "загрузить", "скачай",
        # Spanish
        "descargar", "bajar", "descarga",
        # French
        "telecharger", "télécharger", "telecharge",
        # German
        "herunterladen", "runterladen", "download",
        # Italian
        "scaricare", "scarica",
        # Portuguese
        "baixar", "descarregar",
        # Turkish
        "indir", "indirmek",
        # Chinese
        "下载", "下載",
        # Japanese
        "ダウンロード",
        # Korean
        "다운로드",
    ], 0.85),

    ("download_profile", [
        # English
        "download profile", "download channel", "download all from", "bulk download",
        "all posts", "all photos", "all videos from", "download gallery",
        # Hebrew
        "הורד פרופיל", "הורד את כל", "הורד כל", "תוריד פרופיל",
        "תוריד את כל התמונות", "הורד גלריה",
        # Spanish
        "descargar perfil", "descargar todo",
        # French
        "telecharger profil", "telecharger tout",
        # German
        "profil herunterladen", "alles herunterladen",
        # Russian
        "скачать профиль", "скачать все",
        # Arabic
        "تحميل الملف الشخصي", "تحميل الكل",
        # Chinese
        "下载个人资料", "批量下载",
        # Japanese
        "プロフィールをダウンロード",
        # Korean
        "프로필 다운로드",
    ], 0.80),

    ("screenshot", [
        # English
        "screenshot", "screen shot", "capture screenshot", "take screenshot",
        "take a screenshot", "snap", "capture page",
        # Hebrew
        "צלם", "צלם מסך", "לצלם", "תצלם", "תצלם מסך", "צילום מסך", "תכין צילום",
        # Arabic
        "لقطة شاشة", "خذ لقطة",
        # Russian
        "скриншот", "сделай скриншот", "снимок экрана",
        # Spanish
        "captura de pantalla", "hacer screenshot", "capturar pantalla",
        # French
        "capture ecran", "capture d'ecran", "screenshot",
        # German
        "screenshot", "bildschirmfoto", "bildschirm aufnehmen",
        # Italian
        "screenshot", "cattura schermo",
        # Portuguese
        "screenshot", "captura de tela",
        # Turkish
        "ekran görüntüsü", "screenshot al",
        # Chinese
        "截图", "截屏", "屏幕截图",
        # Japanese
        "スクリーンショット", "画面キャプチャ",
        # Korean
        "스크린샷", "화면 캡처",
    ], 0.85),

    ("pdf", [
        # English
        "pdf", "save as pdf", "export pdf", "print to pdf", "make pdf",
        # Hebrew
        "pdf", "שמור כ-pdf", "יצא pdf", "תכין pdf", "לpdf",
        "תשמור כpdf",
        # Spanish
        "guardar como pdf", "exportar pdf",
        # French
        "sauvegarder en pdf", "exporter en pdf",
        # German
        "als pdf speichern", "pdf erstellen",
        # Russian
        "сохранить pdf", "экспорт pdf",
        # Arabic
        "حفظ pdf", "تصدير pdf",
        # Chinese
        "保存为PDF", "导出PDF",
        # Japanese
        "PDFとして保存", "PDF出力",
        # Korean
        "PDF로 저장",
        # Turkish
        "pdf olarak kaydet",
    ], 0.85),

    ("crawl", [
        # English
        "crawl", "crawl site", "crawl website", "capture all pages",
        "full site pdf", "entire site", "whole website",
        # Hebrew
        "זחל", "עבור על כל הדפים", "כל הדפים", "אתר שלם",
        # Spanish
        "rastrear sitio", "capturar todo el sitio",
        # French
        "explorer le site", "capturer tout le site",
        # German
        "website crawlen", "alle seiten",
        # Russian
        "обход сайта", "весь сайт",
        # Arabic
        "زحف الموقع", "كل صفحات",
        # Chinese
        "爬取网站", "整个网站",
        # Japanese
        "サイトクロール",
        # Korean
        "사이트 크롤",
    ], 0.80),

    ("browse", [
        # English
        "browse", "open", "visit", "go to", "show me", "load page", "open page",
        "open url", "show page",
        # Hebrew
        "פתח", "עבור ל", "עבור אל", "תפתח", "תראה לי", "גלוש", "הכנס ל",
        "לך ל", "תלך ל",
        # Spanish
        "abrir", "visitar", "ir a", "navegar",
        # French
        "ouvrir", "visiter", "aller a", "naviguer vers",
        # German
        "oeffnen", "besuchen", "navigieren zu",
        # Russian
        "открыть", "перейти", "посетить",
        # Arabic
        "افتح", "انتقل الى", "زيارة",
        # Chinese
        "打开", "访问", "浏览",
        # Japanese
        "開く", "アクセス", "ブラウズ",
        # Korean
        "열기", "방문", "접속",
    ], 0.60),  # lower confidence - very generic

    ("scrape", [
        # English
        "scrape", "extract", "get data from", "pull data", "harvest",
        "extract table", "get list", "grab data",
        # Hebrew
        "חלץ", "חלץ נתונים", "תשלוף", "שלוף נתונים", "קח נתונים",
        "חפור", "תחפור", "שלוף מ",
        # Spanish
        "extraer", "raspar datos", "obtener datos de",
        # French
        "extraire", "gratter", "recuperer donnees",
        # German
        "extrahieren", "daten kratzen", "daten holen",
        # Russian
        "парсить", "извлечь данные", "скрейпинг",
        # Arabic
        "استخراج", "جمع البيانات",
        # Chinese
        "抓取", "提取数据", "爬取数据",
        # Japanese
        "スクレイピング", "データ抽出",
        # Korean
        "스크래핑", "데이터 추출",
    ], 0.80),

    ("scrape_species", [
        # English
        "species", "insect", "bug", "animal", "plant", "organism",
        "scientific name", "inat", "inaturalist", "gbif", "bugguide",
        "taxonomy", "identify species", "what species",
        # Hebrew
        "מין", "חרק", "בעל חיים", "צמח", "שם מדעי", "זיהוי מין",
        "תחפש חרק", "תחפש מין",
        # Spanish
        "especie", "insecto", "bicho", "nombre cientifico",
        # French
        "espece", "insecte", "nom scientifique",
        # German
        "art", "insekt", "wissenschaftlicher name",
        # Russian
        "вид", "насекомое", "научное название",
        # Arabic
        "نوع", "حشرة", "اسم علمي",
        # Chinese
        "物种", "昆虫", "学名",
        # Japanese
        "種", "昆虫", "学名",
        # Korean
        "종", "곤충", "학명",
    ], 0.80),

    ("accessibility", [
        # English
        "accessibility", "accessible", "wcag", "aria", "a11y",
        "check accessibility", "audit", "screen reader",
        # Hebrew
        "נגישות", "בדוק נגישות", "תבדוק נגישות", "תבדוק שהאתר נגיש",
        "בדיקת נגישות",
        # Spanish
        "accesibilidad", "verificar accesibilidad",
        # French
        "accessibilite", "verifier accessibilite",
        # German
        "barrierefreiheit", "zugaenglichkeit pruefen",
        # Russian
        "доступность", "проверить доступность",
        # Arabic
        "إمكانية الوصول", "التحقق من إمكانية الوصول",
        # Chinese
        "无障碍", "可访问性检查",
        # Japanese
        "アクセシビリティ", "アクセシビリティチェック",
        # Korean
        "접근성", "접근성 검사",
    ], 0.85),

    ("codegen", [
        # English
        "codegen", "record browser", "record actions", "record session",
        "record what i do", "record my browser", "generate test",
        "record interactions",
        # Hebrew
        "הקלט פעולות", "תקליט", "תקליט מה שאני עושה",
        "תקליט פעולות בדפדפן", "הקלטת דפדפן",
        # Spanish
        "grabar navegador", "registrar acciones",
        # French
        "enregistrer navigateur", "enregistrer actions",
        # German
        "browser aufnehmen", "aktionen aufnehmen",
        # Russian
        "записать браузер", "записать действия",
        # Arabic
        "تسجيل المتصفح", "تسجيل الاجراءات",
        # Chinese
        "录制浏览器", "录制操作",
        # Japanese
        "ブラウザ録画", "操作を記録",
        # Korean
        "브라우저 녹화", "동작 기록",
    ], 0.85),

    ("intercept", [
        # English
        "intercept", "network requests", "api calls", "what requests",
        "monitor requests", "log requests", "spy on network",
        "show me all requests", "what api",
        # Hebrew
        "בקשות רשת", "תראה בקשות", "מה הבקשות", "האזן לרשת",
        "בקשות API", "תראה לי את כל הבקשות",
        # Spanish
        "interceptar solicitudes", "peticiones de red",
        # French
        "intercepter requetes", "requetes reseau",
        # German
        "anfragen abfangen", "netzwerkanfragen",
        # Russian
        "перехватить запросы", "сетевые запросы",
        # Arabic
        "اعتراض الطلبات", "طلبات الشبكة",
        # Chinese
        "拦截请求", "网络请求",
        # Japanese
        "リクエスト傍受", "ネットワークリクエスト",
        # Korean
        "요청 가로채기", "네트워크 요청",
    ], 0.85),

    ("test_all", [
        # English
        "test all browsers", "cross browser", "run tests", "test on all",
        "test chromium firefox webkit", "multi browser test",
        # Hebrew
        "בדוק כל הדפדפנים", "ריצה על כל הדפדפנים", "תריץ טסטים",
        "בדיקות על כל הדפדפנים",
        # Spanish
        "probar todos navegadores", "prueba cruzada",
        # French
        "tester tous navigateurs", "test multi navigateurs",
        # German
        "alle browser testen", "cross-browser test",
        # Russian
        "тестировать все браузеры", "кросс браузер",
        # Arabic
        "اختبار كل المتصفحات", "اختبار متعدد المتصفحات",
        # Chinese
        "跨浏览器测试", "所有浏览器",
        # Japanese
        "全ブラウザテスト", "クロスブラウザ",
        # Korean
        "모든 브라우저 테스트",
    ], 0.85),

    ("form", [
        # English
        "fill form", "fill out form", "submit form", "automate form",
        "form automation", "fill in the form",
        # Hebrew
        "מלא טופס", "תמלא טופס", "טופס אוטומטי", "תמלא את הטופס",
        # Spanish
        "llenar formulario", "rellenar formulario",
        # French
        "remplir formulaire", "remplissage formulaire",
        # German
        "formular ausfuellen", "formular automatisch",
        # Russian
        "заполнить форму", "автозаполнение формы",
        # Arabic
        "ملء النموذج", "تعبئة الاستمارة",
        # Chinese
        "填写表单", "自动填表",
        # Japanese
        "フォーム入力", "フォームを自動入力",
        # Korean
        "양식 작성", "폼 자동 입력",
    ], 0.80),

    ("trace", [
        # English
        "trace", "capture trace", "record trace", "playwright trace",
        "performance trace", "network trace",
        # Hebrew
        "עקוב", "תיעוד עקיבה", "trace",
        # Spanish
        "traza", "capturar traza",
        # French
        "tracer", "capturer trace",
        # German
        "trace aufnehmen", "netzwerk trace",
        # Russian
        "трассировка", "записать трассировку",
        # Arabic
        "تتبع", "التقاط التتبع",
        # Chinese
        "追踪", "捕获跟踪",
        # Japanese
        "トレース", "トレース記録",
        # Korean
        "트레이스", "추적 캡처",
    ], 0.75),

    ("api_get", [
        # English
        "api get", "http get", "get request", "fetch api", "call api",
        "test api", "check api", "make a get",
        # Hebrew
        "קריאת API", "בקשת GET", "תשלח GET",
        # Spanish
        "llamada get", "peticion get",
        # French
        "requete get", "appel api get",
        # German
        "get anfrage", "api get aufruf",
        # Russian
        "get запрос", "вызов api",
        # Arabic
        "طلب get", "استدعاء api",
        # Chinese
        "GET请求", "API调用",
        # Japanese
        "GETリクエスト", "API呼び出し",
        # Korean
        "GET 요청", "API 호출",
    ], 0.80),

    ("api_post", [
        # English
        "api post", "http post", "post request", "send post", "post data",
        # Hebrew
        "בקשת POST", "שלח POST",
        # Spanish
        "peticion post", "enviar post",
        # French
        "requete post", "envoyer post",
        # German
        "post anfrage", "post senden",
        # Russian
        "post запрос", "отправить post",
        # Arabic
        "طلب post",
        # Chinese
        "POST请求",
        # Japanese
        "POSTリクエスト",
        # Korean
        "POST 요청",
    ], 0.80),
]

# ---------------------------------------------------------------------------
# Query extraction helpers
# ---------------------------------------------------------------------------

def _extract_species_query(text: str) -> str | None:
    """Extract species/animal name from free text."""
    # Look for quoted strings first
    m = re.search(r'"([^"]+)"', text)
    if m:
        return m.group(1)
    # After named keywords
    for pattern in [
        r"(?:species|insect|bug|animal|plant|חרק|מין|especie|espèce|art|вид)\s+(?:named?|called?|כ|של)?\s*['\"]?([A-Za-z][A-Za-z\s]{2,30})",
        r"(?:about|on|for|על|sobre|sur|uber|über|про)\s+(?:the\s+)?([A-Za-z][A-Za-z\s]{2,30})",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _extract_css_selector(text: str) -> str | None:
    """Extract a CSS selector hint from free text."""
    m = re.search(r'"([.#\[][^"]+)"', text)
    if m:
        return m.group(1)
    m = re.search(r"selector\s+['\"]?([.#\[][^\s'\"]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_file_path(text: str) -> str | None:
    """Extract a file path from free text."""
    m = re.search(r"['\"]([^'\"]+\.py)['\"]", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:file|test|path)\s+(\S+\.py)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Clarifying questions per language
# ---------------------------------------------------------------------------

_CLARIFY_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "no_url": "What URL should I use? Please provide a full URL (e.g., https://example.com).",
        "ambiguous": "I'm not sure what you'd like me to do. Did you mean to: {options}?",
        "no_query": "What species or animal name should I search for?",
        "no_file": "What test file should I run? Please provide the path to a .py file.",
    },
    "he": {
        "no_url": "באיזה URL להשתמש? אנא ספק כתובת מלאה (לדוגמה: https://example.com).",
        "ambiguous": "לא בטוח מה תרצה שאעשה. האם התכוונת ל: {options}?",
        "no_query": "איזה שם מין או בעל חיים לחפש?",
        "no_file": "איזה קובץ טסט להריץ? אנא ציין נתיב לקובץ .py.",
    },
    "ar": {
        "no_url": "ما هو عنوان URL الذي يجب استخدامه؟ يرجى تقديم عنوان URL كامل.",
        "ambiguous": "لست متأكدًا مما تريد القيام به. هل تقصد: {options}؟",
        "no_query": "ما اسم النوع أو الحيوان للبحث عنه؟",
        "no_file": "ما ملف الاختبار الذي يجب تشغيله؟",
    },
    "ru": {
        "no_url": "Какой URL использовать? Пожалуйста, укажите полный URL.",
        "ambiguous": "Не уверен, что вы хотите сделать. Вы имели в виду: {options}?",
        "no_query": "Какое название вида или животного искать?",
        "no_file": "Какой тестовый файл запустить? Укажите путь к .py файлу.",
    },
    "es": {
        "no_url": "Que URL debo usar? Proporciona una URL completa.",
        "ambiguous": "No estoy seguro de que quieres hacer. Quisiste decir: {options}?",
        "no_query": "Que nombre de especie o animal buscar?",
        "no_file": "Que archivo de prueba debo ejecutar?",
    },
    "fr": {
        "no_url": "Quelle URL utiliser? Veuillez fournir une URL complete.",
        "ambiguous": "Je ne suis pas sur de ce que vous voulez faire. Vouliez-vous dire: {options}?",
        "no_query": "Quel nom d'espece ou d'animal rechercher?",
        "no_file": "Quel fichier de test executer?",
    },
    "de": {
        "no_url": "Welche URL soll ich verwenden? Bitte gib eine vollstaendige URL an.",
        "ambiguous": "Ich bin nicht sicher was du tun moechtest. Meintest du: {options}?",
        "no_query": "Nach welchem Arten- oder Tiernamen suchen?",
        "no_file": "Welche Testdatei soll ausgefuehrt werden?",
    },
    "zh": {
        "no_url": "应该使用哪个URL？请提供完整的URL。",
        "ambiguous": "我不确定你想做什么。你是指：{options}吗？",
        "no_query": "要搜索哪个物种或动物名称？",
        "no_file": "要运行哪个测试文件？",
    },
    "ja": {
        "no_url": "どのURLを使用しますか？完全なURLを提供してください。",
        "ambiguous": "何をしたいのかわかりません。{options}のことですか？",
        "no_query": "どの種や動物の名前を検索しますか？",
        "no_file": "どのテストファイルを実行しますか？",
    },
    "ko": {
        "no_url": "어떤 URL을 사용해야 하나요? 전체 URL을 제공해 주세요.",
        "ambiguous": "무엇을 하려는지 확실하지 않습니다. {options}을 의미하셨나요?",
        "no_query": "어떤 종이나 동물 이름을 검색할까요?",
        "no_file": "어떤 테스트 파일을 실행할까요?",
    },
}

_CONFIDENCE_THRESHOLD = 0.65


def _get_clarify(lang: str, key: str, **kwargs) -> str:
    templates = _CLARIFY_TEMPLATES.get(lang, _CLARIFY_TEMPLATES["en"])
    tmpl = templates.get(key, _CLARIFY_TEMPLATES["en"][key])
    return tmpl.format(**kwargs)


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify(text: str) -> Intent:
    """Classify free text into a PSR.ai Intent.

    Uses keyword matching with per-language patterns. Works offline.
    Detects language automatically from character sets and word patterns.
    Returns an Intent with the best-matching action, extracted URL/params,
    confidence score, and (if confidence < threshold) a clarifying question.
    """
    lang = _detect_language(text)
    url = _extract_url(text)
    text_lower = text.lower()

    best_action: str = "browse"
    best_confidence: float = 0.0
    scored: list[tuple[float, str]] = []

    for action, keywords, base_conf in _RULES:
        score = 0.0
        matched_count = 0
        for kw in keywords:
            if kw.lower() in text_lower:
                # Longer keyword matches score higher
                weight = len(kw) / 30.0
                score = max(score, base_conf * min(1.0, 0.5 + weight))
                matched_count += 1

        if matched_count > 1:
            score = min(1.0, score * (1.0 + 0.1 * (matched_count - 1)))

        if score > 0:
            scored.append((score, action))

    if scored:
        scored.sort(key=lambda x: -x[0])
        best_confidence, best_action = scored[0]

    # Build params
    params: dict = {}
    clarification: str | None = None

    if best_action in ("screenshot", "pdf", "browse", "scrape", "accessibility",
                       "codegen", "intercept", "trace", "form", "crawl",
                       "api_get", "api_post"):
        if not url:
            if best_confidence >= _CONFIDENCE_THRESHOLD:
                clarification = _get_clarify(lang, "no_url")
                best_confidence *= 0.6
        else:
            params["url"] = url
            if best_action == "pdf":
                params["as_pdf"] = True

    elif best_action in ("download", "download_profile"):
        if not url:
            clarification = _get_clarify(lang, "no_url")
            best_confidence *= 0.6
        else:
            params["url"] = url
        # Audio-only detection
        audio_keywords = ["audio", "mp3", "music", "שמע", "מוזיקה", "audio", "son", "música"]
        if any(kw in text_lower for kw in audio_keywords):
            params["audio_only"] = True

    elif best_action == "scrape_species":
        query = _extract_species_query(text)
        if query:
            params["query"] = query
        else:
            clarification = _get_clarify(lang, "no_query")
            best_confidence *= 0.6

    elif best_action == "test_all":
        file_path = _extract_file_path(text)
        if file_path:
            params["test_file"] = file_path
        else:
            clarification = _get_clarify(lang, "no_file")
            best_confidence *= 0.7

        # Browser list
        detected_browsers = []
        for b in ("chromium", "firefox", "webkit"):
            if b in text_lower:
                detected_browsers.append(b)
        if detected_browsers:
            params["browsers"] = detected_browsers

    # Selector extraction for scrape
    if best_action == "scrape":
        sel = _extract_css_selector(text)
        if sel:
            params["selector"] = sel

    # Browser override
    for browser in ("firefox", "webkit", "safari", "chromium", "chrome"):
        if browser in text_lower:
            params["browser"] = "webkit" if browser == "safari" else browser
            break

    # Headed mode
    if any(kw in text_lower for kw in ["headed", "visible", "show browser", "with browser", "graphical", "gui"]):
        params["headless"] = False

    # Low confidence -> ambiguous clarification
    if best_confidence < _CONFIDENCE_THRESHOLD and not clarification:
        if scored:
            top_options = ", ".join(a for _, a in scored[:3])
            clarification = _get_clarify(lang, "ambiguous", options=top_options)
        else:
            clarification = _get_clarify(lang, "ambiguous", options="screenshot, download, browse, scrape")

    return Intent(
        action=best_action,
        url=url,
        params=params,
        confidence=round(best_confidence, 3),
        raw=text,
        language=lang,
        clarification=clarification,
    )


# ---------------------------------------------------------------------------
# Dispatcher: Intent -> CLI args
# ---------------------------------------------------------------------------

def intent_to_cli_args(intent: Intent) -> list[str]:
    """Convert a classified Intent into a `sa` CLI argument list.

    Returns a list suitable for passing to the `sa` Click group.
    """
    action = intent.action
    params = intent.params
    url = intent.url or params.get("url", "")

    if action == "screenshot":
        args = ["capture", url]
        if params.get("browser"):
            pass  # capture doesn't take --browser yet
        return args

    if action == "pdf":
        return ["capture", url, "--pdf"]

    if action == "crawl":
        return ["capture", url, "--crawl"]

    if action == "browse":
        return ["browse", url]

    if action == "scrape":
        sel = params.get("selector", "body")
        return ["scrape", url, sel]

    if action == "scrape_species":
        return ["scrape-species", params.get("query", "")]

    if action == "accessibility":
        return ["accessibility", url]

    if action == "codegen":
        args = ["codegen", url]
        if params.get("browser"):
            args += ["--browser", params["browser"]]
        return args

    if action == "intercept":
        return ["intercept", url]

    if action == "test_all":
        args = ["test-all", params.get("test_file", "")]
        if params.get("browsers"):
            args += ["--browsers", ",".join(params["browsers"])]
        return args

    if action == "form":
        return ["form", url]

    if action == "trace":
        return ["trace", url]

    if action == "download":
        args = ["download", url]
        if params.get("audio_only"):
            args.append("--audio")
        return args

    if action == "download_profile":
        return ["download-profile", url]

    if action == "api_get":
        return ["api", url, "--method", "GET"]

    if action == "api_post":
        return ["api", url, "--method", "POST"]

    # Default fallback
    return ["browse", url] if url else ["browse", "--help"]
