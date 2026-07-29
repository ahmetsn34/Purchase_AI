#!/usr/bin/env python3
"""
demo.py — Sistemi mail/telegram/gercek LLM olmadan uctan uca dener.

Iki mod:
  python demo.py              LLM'i taklit eder (SahteLLM) — coklu kalem cikarimi,
                              toplam->birim hesabi, muadil uyarisi TEST EDILIR
  python demo.py --sezgisel   LLM tamamen kapali, regex yedegi ne yapiyor gorulur

SahteLLM, gercek modelin dondurecegi sema-uyumlu ciktiyi elle yaziyor. Yani bu
demo BENIM KODUMU test eder (eslestirme, aritmetik, rapor); gercek modelin metni
ne kadar dogru okudugunu ise config.json'a anahtar koyup calistirinca gorursun.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

KOK = Path(__file__).parent
sys.path.insert(0, str(KOK))

from src import db, suppliers, requests_loader, rfq, reply_parser, compare, telegram_bot
from src.llm import LLM
from main import SahteMail, SahteBot, adim1_tedarikciler, adim2_talepler, adim3_paketler, log

DEMO = KOK / "data" / "demo"


# ------------------------------------------------------------------ sahte LLM

class SahteLLM(LLM):
    """
    Gercek modelin dondurecegi sema-uyumlu ciktilari taklit eder.
    Amac: cikarim SONRASI tum Python mantigini (kalem eslestirme, toplamdan
    birim hesabi, muadil isareti, kademeli fiyat) gercekten sinamak.
    """

    def __init__(self):
        super().__init__({"api_key": "sahte"})
        self.aktif = True

    def arac_cagir(self, sistem, mesaj, arac):
        self.cagri_sayisi += 1
        if arac["name"] == "sartname_temizle":
            return None
        if arac["name"] == "sutun_eslestir":
            return None

        m = mesaj

        # 1) Coklu kalem + muadil + kademeli + bir kalemde stok yok
        if "6205 2RS rulman birim fiyatimiz" in m:
            return {
                "cevap_turu": "kismi_teklif",
                "genel_not": "Odeme 60 gun vadeli. 6206 icin stok yok.",
                "kalemler": [
                    {"kalem_no": 1, "tedarikci_malzeme": "Rulman 6205 2RS",
                     "birim_fiyat": 4.90, "para_birimi": "EUR", "kdv_dahil": False,
                     "teslim_suresi_gun": 15, "min_siparis": 100, "guven": "yuksek",
                     "kademeli": [{"miktar": 500, "fiyat": 4.60},
                                  {"miktar": 1000, "fiyat": 4.35}]},
                    {"kalem_no": 3, "tedarikci_malzeme": "FAG 6304-2RSR",
                     "birim_fiyat": 7.20, "para_birimi": "EUR", "kdv_dahil": False,
                     "teslim_suresi_gun": 7, "muadil": True,
                     "muadil_aciklama": "SKF 6304 yerine FAG 6304-2RSR teklif edildi",
                     "guven": "yuksek"},
                ],
            }

        # 2) Coklu kalem, Excel eki (kalem no yazmamis -> ad ile eslesecek)
        if "Birim Fiyat" in m and "PB" in m:
            return {
                "cevap_turu": "fiyat_teklifi",
                "genel_not": "Tum kalemler stokta.",
                "kalemler": [
                    {"tedarikci_malzeme": "Rulman 6205 2RS", "birim_fiyat": 5.10,
                     "para_birimi": "EUR", "kdv_dahil": False, "teslim_suresi_gun": 10,
                     "guven": "yuksek"},
                    {"tedarikci_malzeme": "Rulman 6206 2RS", "birim_fiyat": 6.15,
                     "para_birimi": "EUR", "kdv_dahil": False, "teslim_suresi_gun": 10,
                     "guven": "yuksek"},
                    {"tedarikci_malzeme": "Rulman 6304 2RS", "birim_fiyat": 6.95,
                     "para_birimi": "EUR", "kdv_dahil": False, "teslim_suresi_gun": 10,
                     "guven": "yuksek"},
                ],
            }

        # 3) SADECE TOPLAM verilmis -> birim fiyati Python hesaplamali
        if "toplam teklifimiz" in m or "64.250" in m:
            return {
                "cevap_turu": "fiyat_teklifi",
                "genel_not": "Makarali teslim dahildir.",
                "kalemler": [
                    {"kalem_no": 1, "tedarikci_malzeme": "NYY 4x16 kablo",
                     "toplam_fiyat": 64250.0, "miktar": 500, "para_birimi": "TRY",
                     "kdv_dahil": False, "teslim_suresi_gun": 10, "teslim_sekli": "EXW",
                     "guven": "yuksek"},
                    {"kalem_no": 2, "tedarikci_malzeme": "NYY 4x25 kablo",
                     "birim_fiyat": 196.0, "para_birimi": "TRY", "kdv_dahil": False,
                     "teslim_suresi_gun": 10, "guven": "yuksek"},
                ],
            }

        # 4) Teklif degil: bilgi istiyor. "2 gun icinde donebiliriz" TESLIM SURESI DEGIL
        if "teknik resim" in m:
            return {"cevap_turu": "bilgi_talebi",
                    "genel_not": "Fiyat icin teknik resim ve St37/St52 kalite bilgisi istiyor",
                    "kalemler": []}

        # 5) Otomatik yanit
        if "yillik izin" in m:
            return {"cevap_turu": "otomatik_yanit",
                    "genel_not": "Firma 1-15 Agustos arasi kapali", "kalemler": []}

        return {"cevap_turu": "belirsiz", "genel_not": "SahteLLM tanimadi", "kalemler": []}


# ------------------------------------------------------------------ hazirlik

def temizle():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    (DEMO / "requests").mkdir(parents=True)
    (DEMO / "attachments").mkdir(parents=True)


def config():
    cfg = json.loads((KOK / "config.example.json").read_text(encoding="utf-8"))
    cfg["paths"] = {"database": "data/demo/demo.db",
                    "requests": "data/demo/requests",
                    "attachments": "data/demo/attachments",
                    "suppliers_csv": "suppliers.example.csv"}
    cfg["currency"]["rates"] = {"TRY": 1.0, "USD": 40.15, "EUR": 46.80, "GBP": 54.10}
    cfg["rfq"]["report_recipients"] = ["satinalma.muduru@ornek.example"]
    return cfg


def talep_dosyasi():
    """6 kalem, 3 kategori -> 3 RFQ grubu olusmali."""
    pd.DataFrame([
        {"MALZEME ADI": "Rulman 6205 2RS", "Teknik Özellik": "SKF veya muadili, C3",
         "İhtiyaç": 200, "Ölçü Birimi": "adet", "Malzeme Grubu": "rulman",
         "Termin": "2026-09-15", "Sevk Yeri": "Kocaeli", "Talep Sahibi": "Bakim", "Not": ""},
        {"MALZEME ADI": "Rulman 6206 2RS", "Teknik Özellik": "SKF veya muadili",
         "İhtiyaç": 50, "Ölçü Birimi": "adet", "Malzeme Grubu": "rulman",
         "Termin": "2026-09-15", "Sevk Yeri": "Kocaeli", "Talep Sahibi": "Bakim", "Not": ""},
        {"MALZEME ADI": "Rulman 6304 2RS", "Teknik Özellik": "SKF marka",
         "İhtiyaç": 30, "Ölçü Birimi": "adet", "Malzeme Grubu": "rulman",
         "Termin": "2026-08-05", "Sevk Yeri": "Kocaeli", "Talep Sahibi": "Bakim", "Not": ""},
        {"MALZEME ADI": "NYY kablo 4x16", "Teknik Özellik": "TSE belgeli, 0,6/1 kV",
         "İhtiyaç": 500, "Ölçü Birimi": "metre", "Malzeme Grubu": "kablo",
         "Termin": "2026-08-30", "Sevk Yeri": "Ankara", "Talep Sahibi": "Elektrik",
         "Not": "Makarali teslim tercih edilir"},
        {"MALZEME ADI": "NYY kablo 4x25", "Teknik Özellik": "TSE belgeli",
         "İhtiyaç": 300, "Ölçü Birimi": "metre", "Malzeme Grubu": "kablo",
         "Termin": "2026-08-30", "Sevk Yeri": "Ankara", "Talep Sahibi": "Elektrik", "Not": ""},
        {"MALZEME ADI": "Sac levha 3mm", "Teknik Özellik": "DKP 1500x3000",
         "İhtiyaç": 40, "Ölçü Birimi": "adet", "Malzeme Grubu": "sac",
         "Termin": "2026-08-10", "Sevk Yeri": "Kocaeli", "Talep Sahibi": "Uretim", "Not": ""},
        {"MALZEME ADI": "", "Teknik Özellik": "", "İhtiyaç": None, "Ölçü Birimi": "",
         "Malzeme Grubu": "", "Termin": "", "Sevk Yeri": "", "Talep Sahibi": "", "Not": ""},
    ]).to_excel(DEMO / "requests" / "temmuz_talep_listesi.xlsx", index=False)


def ek_dosyalari():
    yollar = {}
    x = DEMO / "attachments" / "anadolu_rulman_teklif.xlsx"
    pd.DataFrame([
        {"Malzeme": "Rulman 6205 2RS", "Miktar": 200, "Birim Fiyat": "5,10", "PB": "EUR"},
        {"Malzeme": "Rulman 6206 2RS", "Miktar": 50, "Birim Fiyat": "6,15", "PB": "EUR"},
        {"Malzeme": "Rulman 6304 2RS", "Miktar": 30, "Birim Fiyat": "6,95", "PB": "EUR"},
    ]).to_excel(x, index=False)
    yollar["excel"] = x

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    p = DEMO / "attachments" / "elektrik_teklif.pdf"
    c = canvas.Canvas(str(p), pagesize=A4)
    y = 780
    for satir in ["ORNEK ELEKTRIK SAN. TIC. LTD. STI.", "FIYAT TEKLIFI", "",
                  "Kalem 1: NYY 4x16 kablo, 500 metre",
                  "  500 metre icin toplam teklifimiz 64.250,00 TL (KDV haric)",
                  "Kalem 2: NYY 4x25 kablo, 300 metre",
                  "  Birim fiyat 196,00 TL/metre (KDV haric)", "",
                  "Teslim: 10 is gunu, EXW", "Makarali teslim dahildir."]:
        c.drawString(60, y, satir)
        y -= 20
    c.save()
    yollar["pdf"] = p

    t = DEMO / "attachments" / "taranmis_teklif.pdf"
    c = canvas.Canvas(str(t), pagesize=A4)
    c.rect(60, 600, 400, 150, fill=0)
    c.save()
    yollar["taranmis"] = t
    return yollar


class CevapliMail(SahteMail):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.gonderilenler, self.gelen_kutusu, self.ekler = [], [], {}

    def gonder(self, alicilar, konu, govde, bilgi=None, ekler=None):
        mid, cid = super().gonder(alicilar, konu, govde, bilgi, ekler)
        self.gonderilenler.append({"mail": alicilar[0], "konu": konu, "mid": mid, "cid": cid})
        return mid, cid

    def cevap_ekle(self, gonderen, konu, govde, cid=None, ek_yollari=None):
        mid = f"in-{len(self.gelen_kutusu)+1}"
        self.gelen_kutusu.append({
            "id": mid, "subject": konu,
            "from": {"emailAddress": {"address": gonderen}},
            "receivedDateTime": "2026-07-29T10:00:00Z",
            "body": {"contentType": "Text", "content": govde},
            "bodyPreview": govde[:100], "conversationId": cid,
            "hasAttachments": bool(ek_yollari), "internetMessageId": mid})
        if ek_yollari:
            self.ekler[mid] = ek_yollari

    def okunmamislar(self, limit=50):
        return list(self.gelen_kutusu)

    def ekleri_indir(self, mid, hedef, izin):
        kopyalar, hedef = [], Path(hedef)
        hedef.mkdir(parents=True, exist_ok=True)
        for y in self.ekler.get(mid, []):
            if Path(y).suffix.lower() not in izin:
                continue
            k = hedef / f"{mid}_{Path(y).name}"
            shutil.copy(y, k)
            kopyalar.append(str(k))
        return kopyalar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sezgisel", action="store_true",
                    help="LLM'i tamamen kapat, regex yedegini gor")
    args = ap.parse_args()

    temizle()
    cfg = config()
    talep_dosyasi()
    ekler = ek_dosyalari()

    con = db.baglan(KOK / cfg["paths"]["database"])
    llm = LLM({}) if args.sezgisel else SahteLLM()
    mail, bot = CevapliMail(cfg["email"]), SahteBot()
    print(f"\nMOD: {'SEZGISEL (regex)' if args.sezgisel else 'SAHTE LLM (sema taklidi)'}")

    print("\n" + "=" * 74 + "\n ADIM 1-3: TALEPLERI OKU, GRUPLA, ONAYA SUN\n" + "=" * 74)
    adim1_tedarikciler(con, cfg)
    adim2_talepler(con, cfg, llm)
    adim3_paketler(con, cfg, llm)

    print("\n" + "=" * 74 + "\n ORNEK RFQ MAILI (kalem tablosu)\n" + "=" * 74)
    o1 = con.execute("SELECT * FROM onaylar ORDER BY id LIMIT 1").fetchone()
    print(json.loads(o1["payload"])["alicilar"][0]["govde"])

    for o in con.execute("SELECT * FROM onaylar WHERE durum='bekliyor'").fetchall():
        con.execute("UPDATE onaylar SET durum='onaylandi' WHERE id=?", (o["id"],))
        con.commit()
        rfq.paketi_gonder(con, mail, json.loads(o["payload"]), lambda *a: None)
        con.execute("UPDATE onaylar SET durum='uygulandi' WHERE id=?", (o["id"],))
    con.commit()
    print(f"\n{len(mail.gonderilenler)} RFQ maili gonderildi (dry)")

    g = {x["mail"]: x for x in mail.gonderilenler}

    def cvp(adres, govde, ekler_=None, konu_oneki="RE: "):
        if adres in g:
            h = g[adres]
            mail.cevap_ekle(h["mail"], konu_oneki + h["konu"], govde, h["cid"], ekler_)

    cvp("teklif@ornekrulman.example",
        "Merhaba,\n\n1 numarali kalem icin 6205 2RS rulman birim fiyatimiz 200 adet icin "
        "4,90 EUR + KDV'dir. 500 adet uzeri 4,60 EUR, 1000 adet uzeri 4,35 EUR. "
        "Teslim 15 gun, min siparis 100 adet.\n\n"
        "3 numarali kalemde SKF stokta yok; yerine FAG 6304-2RSR onerebiliriz, "
        "7,20 EUR + KDV, 7 gun.\n\n6206 icin su an stok yok.\n\nOdeme 60 gun vadeli.")

    cvp("satis@anadolurulman.example",
        "Anadolu Rulman teklifimiz ektedir, tum kalemler stokta.",
        [str(ekler["excel"])])

    cvp("info@ornekelektrik.example", "Teklifimiz ektedir.", [str(ekler["pdf"])])

    cvp("teklif@bariskablo.example",
        "Fiyatlandirma icin teknik resim ve malzeme kalitesi (St37 mi St52 mi) "
        "bilgisine ihtiyacimiz var. Iletirseniz 2 gun icinde donebiliriz.")

    cvp("satis@ornekcelik.example", "Teklifimiz ekte.", [str(ekler["taranmis"])])

    mail.cevap_ekle("pazarlama@bilinmeyen.example", "Yeni urun katalogumuz",
                    "Katalogumuzu incelemenizi rica ederiz.")

    print("\n" + "=" * 74 + "\n ADIM 5: CEVAPLARI ISLE\n" + "=" * 74)
    islenen, teklif, sahipsiz, cagri = reply_parser.isle(con, mail, llm, cfg, log)
    log(f"islenen={islenen} teklif_satiri={teklif} sahipsiz={sahipsiz} llm_cagrisi={cagri}")

    print("\n" + "=" * 74 + "\n KARSILASTIRMA RAPORLARI\n" + "=" * 74)
    for grup in con.execute("SELECT * FROM talep_gruplari ORDER BY id").fetchall():
        print()
        print(compare.rapor(con, cfg, grup))
        print()

    print("=" * 74)
    for tablo in ("talep_gruplari", "talep_kalemleri", "gonderimler", "cevaplar",
                  "teklifler", "onaylar", "olaylar"):
        n = con.execute(f"SELECT COUNT(*) n FROM {tablo}").fetchone()["n"]
        print(f"  {tablo}: {n}")
    con.close()


if __name__ == "__main__":
    main()
