"""
db.py — SQLite semasi ve erisim katmani.

v2 semasi: bir RFQ artik TEK KALEM degil, KALEM GRUBU. Gercek satin alma
boyle calisir — ayni tedarikciye 5 kalem icin 5 mail atilmaz, tek mailde
kalem tablosu gonderilir.

  talep_gruplari    bir RFQ (RFQ-2026-0001), N kalem icerir
  talep_kalemleri   gruptaki tek tek malzemeler (mailde 1,2,3... numarali)
  tedarikciler      suppliers.csv'den senkronlanir
  gonderimler       grup x tedarikci (message_id, conversation_id)
  cevaplar          gelen her mailin siniflandirmasi (fiyat / stok yok / ...)
  teklifler         kalem bazinda cikarilan fiyat satirlari
  islenen_mailler   idempotency
  onaylar           Telegram onay kuyrugu
  olaylar           basit olay hatti (ekosistem icin)
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS talep_gruplari (
    id INTEGER PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,
    baslik TEXT,
    kategori TEXT,
    teslim_tarihi TEXT,
    durum TEXT DEFAULT 'yeni',
    kaynak_dosya TEXT,
    olusturma TEXT
);

CREATE TABLE IF NOT EXISTS talep_kalemleri (
    id INTEGER PRIMARY KEY,
    grup_id INTEGER NOT NULL,
    sira INTEGER NOT NULL,
    malzeme TEXT NOT NULL,
    sartname TEXT,
    miktar REAL,
    birim TEXT,
    kategori TEXT,
    teslim_tarihi TEXT,
    teslim_yeri TEXT,
    talep_eden TEXT,
    notlar TEXT,
    satir_hash TEXT UNIQUE,
    FOREIGN KEY(grup_id) REFERENCES talep_gruplari(id)
);

CREATE TABLE IF NOT EXISTS tedarikciler (
    id INTEGER PRIMARY KEY,
    firma TEXT NOT NULL,
    mail TEXT UNIQUE NOT NULL,
    yetkili TEXT,
    kategoriler TEXT,
    para_birimi TEXT,
    aktif INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS gonderimler (
    id INTEGER PRIMARY KEY,
    grup_id INTEGER NOT NULL,
    tedarikci_id INTEGER NOT NULL,
    gonderim_tarihi TEXT,
    message_id TEXT,
    conversation_id TEXT,
    hatirlatma_sayisi INTEGER DEFAULT 0,
    son_hatirlatma TEXT,
    durum TEXT DEFAULT 'bekliyor',
    UNIQUE(grup_id, tedarikci_id),
    FOREIGN KEY(grup_id) REFERENCES talep_gruplari(id),
    FOREIGN KEY(tedarikci_id) REFERENCES tedarikciler(id)
);

CREATE TABLE IF NOT EXISTS cevaplar (
    id INTEGER PRIMARY KEY,
    gonderim_id INTEGER NOT NULL,
    message_id TEXT,
    cevap_turu TEXT,
    genel_not TEXT,
    llm_kullanildi INTEGER DEFAULT 0,
    alinma_tarihi TEXT,
    FOREIGN KEY(gonderim_id) REFERENCES gonderimler(id)
);

CREATE TABLE IF NOT EXISTS teklifler (
    id INTEGER PRIMARY KEY,
    gonderim_id INTEGER NOT NULL,
    kalem_id INTEGER,
    cevap_id INTEGER,
    tedarikci_malzeme TEXT,
    birim_fiyat REAL,
    toplam_fiyat REAL,
    para_birimi TEXT,
    kdv_dahil INTEGER,
    miktar REAL,
    teslim_suresi_gun INTEGER,
    min_siparis REAL,
    gecerlilik TEXT,
    teslim_sekli TEXT,
    muadil INTEGER DEFAULT 0,
    muadil_aciklama TEXT,
    kademeli TEXT,
    aciklama TEXT,
    guven TEXT,
    kaynak TEXT,
    kaynak_dosya TEXT,
    ham_metin TEXT,
    eslesme_notu TEXT,
    alinma_tarihi TEXT,
    FOREIGN KEY(gonderim_id) REFERENCES gonderimler(id),
    FOREIGN KEY(kalem_id) REFERENCES talep_kalemleri(id)
);

CREATE TABLE IF NOT EXISTS islenen_mailler (
    message_id TEXT PRIMARY KEY,
    islenme_tarihi TEXT
);

CREATE TABLE IF NOT EXISTS onaylar (
    id INTEGER PRIMARY KEY,
    tur TEXT NOT NULL,
    grup_id INTEGER,
    ozet TEXT,
    payload TEXT,
    durum TEXT DEFAULT 'bekliyor',
    telegram_message_id TEXT,
    olusturma TEXT,
    karar_tarihi TEXT
);

CREATE TABLE IF NOT EXISTS olaylar (
    id INTEGER PRIMARY KEY,
    tur TEXT NOT NULL,
    veri TEXT,
    kaynak_agent TEXT,
    olusturma TEXT,
    islendi INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sayaclar (
    ad TEXT PRIMARY KEY,
    deger INTEGER
);

CREATE INDEX IF NOT EXISTS ix_kalem_grup ON talep_kalemleri(grup_id);
CREATE INDEX IF NOT EXISTS ix_teklif_gonderim ON teklifler(gonderim_id);
CREATE INDEX IF NOT EXISTS ix_gonderim_conv ON gonderimler(conversation_id);
"""


def baglan(yol):
    Path(yol).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(yol, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def simdi():
    return datetime.now().isoformat(timespec="seconds")


def yeni_ref(con, onek=None):
    """RFQ-2026-0001 formatinda, atlamayan sira numarasi."""
    yil = datetime.now().year
    ad = f"rfq_{yil}"
    row = con.execute("SELECT deger FROM sayaclar WHERE ad=?", (ad,)).fetchone()
    deger = (row["deger"] if row else 0) + 1
    con.execute("INSERT INTO sayaclar(ad, deger) VALUES(?,?) "
                "ON CONFLICT(ad) DO UPDATE SET deger=excluded.deger", (ad, deger))
    con.commit()
    return f"{onek or 'RFQ'}-{yil}-{deger:04d}"


def olay_yaz(con, tur, veri, kaynak="rfq-agent"):
    con.execute("INSERT INTO olaylar(tur, veri, kaynak_agent, olusturma) VALUES(?,?,?,?)",
                (tur, json.dumps(veri, ensure_ascii=False), kaynak, simdi()))
    con.commit()


def mail_islendi_mi(con, message_id):
    return con.execute("SELECT 1 FROM islenen_mailler WHERE message_id=?",
                       (message_id,)).fetchone() is not None


def mail_islendi_isaretle(con, message_id):
    con.execute("INSERT OR IGNORE INTO islenen_mailler(message_id, islenme_tarihi) VALUES(?,?)",
                (message_id, simdi()))
    con.commit()


def grup_kalemleri(con, grup_id):
    return con.execute("SELECT * FROM talep_kalemleri WHERE grup_id=? ORDER BY sira",
                       (grup_id,)).fetchall()
