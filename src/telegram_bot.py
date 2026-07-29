"""
telegram_bot.py — Onay kuyrugu ve komutlar.

MeetBot'taki Telegram katmaninin ayni deseni, ama burada isi daha kritik:
mail SIRKET DISINA gidiyor, o yuzden gonderim insan onayina bagli.

Akis:
  1) main.py yeni talep icin paket hazirlar -> onaylar tablosuna 'bekliyor' yazar
  2) bot ozeti Telegram'a atar ("/onay 12" veya "/red 12")
  3) sonraki calismada komutlar okunur, onaylanan paket gonderilir

Komut yorumlama LLM'e verilmemistir: "onay" kelimesini yanlis anlayan bir
model 6 tedarikciye mail atar. Sade, kati bir komut ayristirici kullaniyoruz.
"""

import json
import re
import requests

from . import db, compare

API = "https://api.telegram.org/bot{}/{}"


class Bot:
    def __init__(self, cfg):
        self.token = (cfg.get("token") or "").strip()
        self.chat_id = str(cfg.get("chat_id") or "").strip()
        self.aktif = bool(self.token and self.chat_id)
        self._offset_dosyasi = cfg.get("offset_file", "data/tg_offset.json")

    def yaz(self, metin):
        if not self.aktif:
            return None
        try:
            r = requests.post(API.format(self.token, "sendMessage"),
                              json={"chat_id": self.chat_id, "text": metin[:4000],
                                    "disable_web_page_preview": True}, timeout=30)
            return (r.json().get("result") or {}).get("message_id")
        except Exception:
            return None

    def _offset(self, yeni=None):
        from pathlib import Path
        p = Path(self._offset_dosyasi)
        if yeni is None:
            try:
                return json.loads(p.read_text())["offset"]
            except Exception:
                return None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"offset": yeni}))

    def komutlari_al(self):
        """Yeni mesajlari cekip (metin, chat_id) listesi dondurur."""
        if not self.aktif:
            return []
        params = {"timeout": 0}
        off = self._offset()
        if off:
            params["offset"] = off
        try:
            r = requests.get(API.format(self.token, "getUpdates"), params=params, timeout=40)
            gelenler = r.json().get("result", [])
        except Exception:
            return []
        mesajlar, son = [], off
        for u in gelenler:
            son = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue  # yalnizca yetkili sohbet
            if msg.get("text"):
                mesajlar.append(msg["text"].strip())
        if son:
            self._offset(son)
        return mesajlar


# ------------------------------------------------------------ onay kuyrugu

def onay_olustur(con, tur, grup_id, ozet, payload):
    cur = con.execute(
        """INSERT INTO onaylar(tur, grup_id, ozet, payload, durum, olusturma)
           VALUES(?,?,?,?, 'bekliyor', ?)""",
        (tur, grup_id, ozet, json.dumps(payload, ensure_ascii=False), db.simdi()))
    con.commit()
    return cur.lastrowid


def bekleyenleri_duyur(con, bot, log=print):
    """Henuz Telegram'a atilmamis onaylari gonderir."""
    n = 0
    for o in con.execute(
            "SELECT * FROM onaylar WHERE durum='bekliyor' AND telegram_message_id IS NULL"
    ).fetchall():
        p = json.loads(o["payload"])
        alicilar = p.get("alicilar", [])
        metin = (f"ONAY GEREKIYOR  #{o['id']}\n"
                 f"{o['ozet']}\n\n"
                 f"Gidecek tedarikciler ({len(alicilar)}):\n"
                 + "\n".join(f"  - {a['firma']} <{a['mail']}>" for a in alicilar)
                 + f"\n\nKonu: {p.get('konu','')}\n\n"
                 f"Onayla: /onay {o['id']}\n"
                 f"Reddet: /red {o['id']}\n"
                 f"Mail metnini gor: /goster {o['id']}")
        mid = bot.yaz(metin)
        if mid:
            con.execute("UPDATE onaylar SET telegram_message_id=? WHERE id=?", (str(mid), o["id"]))
            n += 1
    con.commit()
    return n


def komutlari_isle(con, bot, cfg, log=print):
    """
    Telegram komutlarini uygular. Doner: onaylanan onay id listesi.
    Desteklenen: /onay N, /red N, /goster N, /durum, /talepler, /rapor REF, /yardim
    """
    onaylanan = []
    for metin in bot.komutlari_al():
        m = re.match(r"^/(onay|red|goster)\s+#?(\d+)", metin, re.I)
        if m:
            komut, oid = m.group(1).lower(), int(m.group(2))
            o = con.execute("SELECT * FROM onaylar WHERE id=?", (oid,)).fetchone()
            if not o:
                bot.yaz(f"#{oid} bulunamadi.")
                continue
            if komut == "goster":
                p = json.loads(o["payload"])
                ilk = (p.get("alicilar") or [{}])[0]
                bot.yaz(f"#{oid} ornek mail ({ilk.get('firma','')}):\n\n"
                        f"{ilk.get('govde','(bos)')}")
                continue
            if o["durum"] != "bekliyor":
                bot.yaz(f"#{oid} zaten '{o['durum']}' durumunda.")
                continue
            yeni = "onaylandi" if komut == "onay" else "reddedildi"
            con.execute("UPDATE onaylar SET durum=?, karar_tarihi=? WHERE id=?",
                        (yeni, db.simdi(), oid))
            if komut == "red" and o["grup_id"]:
                con.execute("UPDATE talep_gruplari SET durum='iptal' WHERE id=?",
                            (o["grup_id"],))
            con.commit()
            bot.yaz(f"#{oid} {yeni}.")
            if komut == "onay":
                onaylanan.append(oid)
            continue

        k = metin.lower().strip()
        if k.startswith("/durum"):
            s = con.execute(
                "SELECT durum, COUNT(*) n FROM talep_gruplari GROUP BY durum").fetchall()
            bekleyen = con.execute(
                "SELECT COUNT(*) n FROM onaylar WHERE durum='bekliyor'").fetchone()["n"]
            teklif = con.execute("SELECT COUNT(*) n FROM teklifler").fetchone()["n"]
            bot.yaz("DURUM\n" + "\n".join(f"  {r['durum']}: {r['n']}" for r in s)
                    + f"\n  onay bekleyen: {bekleyen}\n  toplam teklif: {teklif}")
        elif k.startswith("/talepler"):
            rows = con.execute(
                """SELECT t.ref, t.baslik, t.durum,
                          (SELECT COUNT(*) FROM talep_kalemleri k WHERE k.grup_id=t.id) kalem
                   FROM talep_gruplari t
                   WHERE t.durum NOT IN ('kapandi','iptal')
                   ORDER BY t.id DESC LIMIT 20""").fetchall()
            bot.yaz("ACIK TALEPLER\n" + ("\n".join(
                f"  {r['ref']}  {r['baslik'][:32]}  {r['kalem']} kalem  [{r['durum']}]"
                for r in rows) or "  yok"))
        elif k.startswith("/rapor"):
            m2 = re.search(r"(RFQ|TKF)-\d{4}-\d{3,6}", metin, re.I)
            if not m2:
                bot.yaz("Kullanim: /rapor RFQ-2026-0001")
                continue
            grup = con.execute("SELECT * FROM talep_gruplari WHERE ref=?",
                               (m2.group(0).upper(),)).fetchone()
            if not grup:
                bot.yaz("Talep bulunamadi.")
                continue
            bot.yaz(compare.rapor(con, cfg, grup))
        elif k.startswith("/yardim") or k.startswith("/help"):
            bot.yaz("Komutlar:\n  /onay N\n  /red N\n  /goster N\n  /durum\n"
                    "  /talepler\n  /rapor RFQ-2026-0001")
    return onaylanan
