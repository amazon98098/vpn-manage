"""Unit tests for NetPlus message parsing using real sample wording."""
from app.vendors.netplus_parser import parse_netplus


def test_alert_days():
    text = (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "📌 از مهلت زمانی استفاده از سرویس 362_sagheb فقط 3 روز باقی مانده است. "
        "لطفاً در صورت تمایل برای تمدید این سرویس، از طریق بخش "
        "«🛍 مشاهده سرویس های خریداری شده من» اقدام بفرمایین. با تشکر از همراهی شما"
    )
    buttons = [[{"text": "💊 تمدید سرویس", "callback_data": "extend_1540cfe1"}]]
    p = parse_netplus(text, buttons)
    assert p.kind == "alert_days"
    assert p.username == "362_sagheb"
    assert p.remaining_days == 3
    assert p.vendor_service_id == "1540cfe1"
    assert p.notify is True


def test_alert_mb():
    text = (
        "با سلام خدمت شما کاربر گرامی 👋\n"
        "🚨 از حجم سرویس 620_goodarzi_amlak تنها 808.44 مگابایت باقی مانده است."
    )
    buttons = [[{"text": "💊 تمدید سرویس", "callback_data": "extend_f844953d"}]]
    p = parse_netplus(text, buttons)
    assert p.kind == "alert_mb"
    assert p.username == "620_goodarzi_amlak"
    assert abs(p.remaining_traffic_mb - 808.44) < 0.01
    assert p.vendor_service_id == "f844953d"


def test_alert_deleted():
    text = (
        "📌 کاربر گرامی بدلیل عدم تمدید، سرویس 0278_ava_beirami "
        "از لیست سرویس های شما حذف گردید"
    )
    p = parse_netplus(text)
    assert p.kind == "alert_deleted"
    assert p.username == "0278_ava_beirami"


def test_renew_ok():
    text = (
        "✅ تمدید برای سرویس شما با موفقیت صورت گرفت\n \n"
        "▫️نام سرویس : 479_karampoor_father\n"
        "▫️نام محصول : 25 گیگ کاربرنامحدود یکماهه🔸 70 هزار تومن\n"
        "▫️مبلغ تمدید 70,000 تومان\n"
        "▫️تاریخ اتمام : ۱۴۰۵/۰۹/۱۲"
    )
    p = parse_netplus(text)
    assert p.kind == "renew_ok"
    assert p.username == "479_karampoor_father"
    assert p.amount_toman == 70000
    assert p.expiration_date is not None


def test_payment_instructions():
    text = (
        "⚠️ مبلغ را دقیق و تا رقم آخر واریز کنید؛ رند نکنید.\n\n"
        "💰 مبلغ: 700790 ریال\n\n"
        "💳 شماره کارت: 5041721256102998\n\n"
        "👤 به نام: زرگل پورحسین"
    )
    p = parse_netplus(text)
    assert p.kind == "payment_instructions"
    assert p.amount_rial == 700790
    assert p.card_number == "5041721256102998"
