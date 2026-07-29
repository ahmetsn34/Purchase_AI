#!/usr/bin/env python3
"""
main.py — Satin alma teklif agent'i. Cron ile gunde 2-3 kez calisir.

Dongu:
  1  tedarikci listesini senkronla
  2  data/requests/ icindeki Excel/CSV talepleri oku
  3  yeni talepler icin tedarikci sec + mail paketi hazirla -> ONAY KUYRUGU
  4  Telegram: onaylari duyur, komutlari isle, onaylananlari GONDER
  5  gelen cevaplari oku (govde + PDF/Excel ekleri) -> teklifleri cikar
  6  cevap gelmeyenlere hatirlatma
  7  suresi dolan talepleri kapat -> karsilastirma raporunu mail at

Kullanim:
  python main.py              tam dongu
  python main.py --dry-run    hicbir mail/telegram gitmez, ekrana yazar
  python main.py --adim 5     tek adim calistir
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src import db, suppliers, requests_loader, rfq, reply_parser, compare, telegram_bot
from src.llm import LLM
from src.graph_mail import Mail

KOK = Path(__file__).parent


def log(*a):
    print(f"[{datetime.now():%H:%M:%S}]", *a, flush=True)


# --------------------------------------------------------- dry-run taklitleri

class SahteMail:
    def __init__(self, cfg):
        self.kutu = cfg.get("mailbox", "teklif@ornek.com")
        self.sayac = 0

    def gonder(self, alicilar, konu, govde, bilgi=None, ekler=None):
        self.sayac += 1
        log(f"  [DRY] mail -> {', '.join(alicilar)} | {konu}")
        print("  " + "\n  ".join(govde.splitlines()[:6]) + "\n  ...")
        return f"dry-msg-{self.sayac}", f"dry-conv-{self.sayac}"

    def cevapla(self, message_id, metin):
        log(f"  [DRY] hatirlatma -> {message_id}")

    def okunmamislar(self, limit=50):
        return []

    def ekleri_indir(self, mid, hedef, izin):
        return []

    def okundu_isaretle(self, mid):
        pass


class SahteBot:
    aktif = True

    def yaz(self, metin):
        print("--- TELEGRAM ---")
        print(metin)
        print("----------------")
        return "dry"

    def komutlari_al(self):
        return []


# --------------------------------------------------------- adimlar

def adim1_tedarikciler(con, cfg):
    n = suppliers.senkronla(con, KOK / cfg["paths"]["suppliers_csv"])
    log(f"1) tedarikci senkron: {n} kayit")


def adim2_talepler(con, cfg, llm):
    log("2) talep dosyalari okunuyor")
    yeni = requests_loader.yukle(con, KOK / cfg["paths"]["requests"], llm,
                                 cfg["rfq"].get("ref_prefix", "RFQ"), log)
    log(f"   yeni RFQ grubu: {len(yeni)}" + (f" -> {', '.join(yeni)}" if yeni else ""))
    for ref in yeni:
        db.olay_yaz(con, "talep_olusturuldu", {"ref": ref})


def adim3_paketler(con, cfg, llm):
    log("3) onay paketleri hazirlaniyor")
    yeniler = con.execute(
        "SELECT * FROM talep_gruplari WHERE durum='yeni' ORDER BY id").fetchall()
    if not yeniler:
        log("   bekleyen yeni RFQ yok")
        return
    en_fazla = int(cfg["rfq"].get("suppliers_per_request", 5))
    for grup in yeniler:
        kalemler = db.grup_kalemleri(con, grup["id"])
        adaylar = suppliers.bul(con, grup["kategori"],
                                [k["malzeme"] for k in kalemler], en_fazla)
        if not adaylar:
            log(f"   ! {grup['ref']} ({grup['baslik'][:40]}): uygun tedarikci bulunamadi")
            continue
        paket = rfq.paket_hazirla(con, cfg, llm, grup, kalemler, adaylar)
        def _kalem_satiri(k):
            m = ""
            if k["miktar"]:
                m = f" ({k['miktar']:g} {k['birim'] or ''})".replace(" )", ")")
            return f"  {k['sira']}. {k['malzeme'][:38]}{m}"

        kalem_ozet = "\n".join(_kalem_satiri(k) for k in kalemler[:8])
        if len(kalemler) > 8:
            kalem_ozet += f"\n  ... +{len(kalemler)-8} kalem"
        ozet = (f"{grup['ref']}  {grup['baslik']}\n"
                f"{len(kalemler)} kalem:\n{kalem_ozet}"
                + (f"\nEn erken termin: {grup['teslim_tarihi']}"
                   if grup["teslim_tarihi"] else "")
                + f"\nKaynak: {grup['kaynak_dosya']}")
        oid = telegram_bot.onay_olustur(con, "rfq_gonderim", grup["id"], ozet, paket)
        con.execute("UPDATE talep_gruplari SET durum='onay_bekliyor' WHERE id=?",
                    (grup["id"],))
        con.commit()
        log(f"   + onay #{oid}: {grup['ref']} ({len(kalemler)} kalem) -> "
            f"{len(adaylar)} tedarikci ({', '.join(a['firma'] for a in adaylar)})")


def adim4_onaylar(con, cfg, bot, mail_client):
    log("4) onaylar")
    duyurulan = telegram_bot.bekleyenleri_duyur(con, bot, log)
    if duyurulan:
        log(f"   {duyurulan} onay Telegram'a iletildi")
    onaylanan = telegram_bot.komutlari_isle(con, bot, cfg, log)
    for oid in onaylanan:
        o = con.execute("SELECT * FROM onaylar WHERE id=?", (oid,)).fetchone()
        paket = json.loads(o["payload"])
        log(f"   onay #{oid} uygulaniyor ({paket['ref']})")
        n = rfq.paketi_gonder(con, mail_client, paket, log)
        con.execute("UPDATE onaylar SET durum='uygulandi' WHERE id=?", (oid,))
        con.commit()
        bot.yaz(f"{paket['ref']}: {n} tedarikciye teklif talebi gonderildi.")
    if not duyurulan and not onaylanan:
        log("   islenecek onay yok")


def adim5_cevaplar(con, cfg, llm, mail_client):
    log("5) gelen cevaplar okunuyor")
    islenen, teklif, sahipsiz, llm_cagri = reply_parser.isle(con, mail_client, llm, cfg, log)
    log(f"   {islenen} mail islendi, {teklif} teklif satiri, {sahipsiz} eslesmeyen, "
        f"{llm_cagri} LLM cagrisi ({llm.hata_sayisi} hata)")


def adim6_hatirlatmalar(con, cfg, mail_client):
    log("6) hatirlatmalar")
    n = rfq.hatirlat(con, mail_client, cfg, log)
    log(f"   {n} hatirlatma")


def adim7_kapat(con, cfg, mail_client, bot):
    log("7) suresi dolan RFQ'lar")
    gun = int(cfg["rfq"].get("response_days", 5)) + int(cfg["rfq"].get("grace_days", 1))
    esik = (datetime.now() - timedelta(days=gun)).isoformat(timespec="seconds")
    hedefler = con.execute(
        """SELECT DISTINCT t.* FROM talep_gruplari t JOIN gonderimler g ON g.grup_id=t.id
           WHERE t.durum IN ('gonderildi','toplaniyor') AND g.gonderim_tarihi < ?""",
        (esik,)).fetchall()
    if not hedefler:
        log("   kapanacak RFQ yok")
        return

    alicilar = cfg["rfq"].get("report_recipients", [])
    for grup in hedefler:
        metin = compare.rapor(con, cfg, grup)
        n = con.execute("""SELECT COUNT(*) n FROM teklifler q
                           JOIN gonderimler g ON g.id=q.gonderim_id
                           WHERE g.grup_id=? AND q.birim_fiyat IS NOT NULL""",
                        (grup["id"],)).fetchone()["n"]
        try:
            if alicilar:
                mail_client.gonder(alicilar,
                                   f"[{grup['ref']}] Teklif karsilastirma — "
                                   f"{grup['baslik'][:60]}", metin)
            con.execute("UPDATE talep_gruplari SET durum='kapandi' WHERE id=?", (grup["id"],))
            db.olay_yaz(con, "talep_kapandi", {"ref": grup["ref"], "teklif_sayisi": n})
            log(f"   * {grup['ref']} kapandi, {n} fiyatli teklif satiriyla rapor gonderildi")
            bot.yaz(f"{grup['ref']} kapandi — {n} fiyatli teklif satiri. Rapor "
                    f"{', '.join(alicilar) or '(alici tanimli degil)'} adresine gitti.")
        except Exception as e:
            log(f"   ! {grup['ref']} raporu gonderilemedi: {e}")
    con.commit()


# --------------------------------------------------------- giris

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="hicbir mail/telegram gonderilmez")
    ap.add_argument("--adim", type=int, help="sadece bu adimi calistir (1-7)")
    args = ap.parse_args()

    cfg_yolu = KOK / args.config
    if not cfg_yolu.exists():
        print(f"HATA: {cfg_yolu.name} yok. config.example.json'i kopyalayip doldurun.")
        sys.exit(1)
    cfg = json.loads(cfg_yolu.read_text(encoding="utf-8"))

    con = db.baglan(KOK / cfg["paths"]["database"])
    llm = LLM(cfg.get("llm", {}))
    if not llm.aktif:
        log("UYARI: LLM anahtari yok — sezgisel (regex) cikarim kullanilacak")

    if args.dry_run:
        mail_client, bot = SahteMail(cfg.get("email", {})), SahteBot()
        log("DRY-RUN: hicbir mail veya Telegram mesaji gonderilmeyecek")
    else:
        mail_client = Mail(cfg["email"])
        bot = telegram_bot.Bot(cfg.get("telegram", {}))
        if not bot.aktif:
            log("HATA: Telegram yapilandirilmamis — onaysiz gonderim yapmiyoruz.")
            sys.exit(1)

    adimlar = {
        1: lambda: adim1_tedarikciler(con, cfg),
        2: lambda: adim2_talepler(con, cfg, llm),
        3: lambda: adim3_paketler(con, cfg, llm),
        4: lambda: adim4_onaylar(con, cfg, bot, mail_client),
        5: lambda: adim5_cevaplar(con, cfg, llm, mail_client),
        6: lambda: adim6_hatirlatmalar(con, cfg, mail_client),
        7: lambda: adim7_kapat(con, cfg, mail_client, bot),
    }

    sira = [args.adim] if args.adim else sorted(adimlar)
    for i in sira:
        try:
            adimlar[i]()
        except Exception as e:
            log(f"! adim {i} hata verdi: {type(e).__name__}: {e}")
            # Bir adimin patlamasi digerlerini durdurmaz.
    con.close()
    log("bitti")


if __name__ == "__main__":
    main()
