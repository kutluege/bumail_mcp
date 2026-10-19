<!-- mcp-name: io.github.kutluege/posta-mcp -->

# posta-mcp (Boğaziçi Postanesi)

English: [docs/README.en.md](docs/README.en.md)

Claude'un e-postanı okumasını, aramasını, düzenlemesini, özetlemesini ve taslak yazmasını sağlayan küçük bir program. Kendi bilgisayarında çalışır ve doğrudan posta sunucusuna bağlanır; arada başka bir servis yoktur. Boğaziçi Üniversitesi (BUmail) için hazırlandı; başka IMAP/SMTP hesaplarıyla da Claude Code üzerinden kullanılabilir.

> **Durum: alfa.** Öğrenci hesapları gerçek sunucuda denendi ve çalışıyor. Personel hesapları ve posta listesi kuralları henüz denenmedi.

Claude Desktop ve Claude Code ile çalışır. claude.ai web sitesinde ve mobil uygulamada çalışmaz.

## Kurulum

### Claude Desktop (terminal gerekmez)

1. [Sürümler sayfasından](https://github.com/kutluege/bumail_mcp/releases) `posta-mcp-<sürüm>.mcpb` dosyasını indir.
2. Dosyaya çift tıkla (açılmazsa Claude Desktop'ta Ayarlar > Uzantılar sayfasına sürükle).
3. E-posta adresini ve BUmail parolanı gir. "E-posta göndermeye izin ver" kutusunu boş bırak.
4. Yeni bir sohbette `/posta:baslangic` yaz.

Parolanı sonradan Ayarlar > Uzantılar > Boğaziçi Postanesi bölümünden değiştirebilirsin. Uzantı açılmazsa aşağıdaki gibi [uv](https://docs.astral.sh/uv/) kurup Claude Desktop'u yeniden başlat.

### Claude Code

Gerekenler: [git](https://git-scm.com/) ve [uv](https://docs.astral.sh/uv/). uv yoksa bir kez kur, sonra yeni bir terminal aç:

```
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Sonra kurulumu başlat:

```
uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp setup
```

Program e-postanı ve parolanı sorar, girişi dener ve sadece giriş başarılı olunca kaydeder. Parola işletim sisteminin anahtar zincirinde saklanır. En sonda "Claude Code'a şimdi ekleyeyim mi?" diye sorar; "E" de. Claude Code'u yeniden başlat ve `/posta:baslangic` yaz.

Diğer komutlar da aynı şekilde çalışır: `uvx --from git+https://github.com/kutluege/bumail_mcp posta-mcp <komut>`

| Komut | Ne yapar |
|---|---|
| `setup` | Hesabı kurar ya da baştan kurar |
| `parola` | Sadece parolayı günceller (örneğin süresi dolduysa) |
| `doctor` | Ayarları ve girişi kontrol eder; sorun varsa ne yapacağını yazar |
| `forget` | Bu bilgisayardaki parolayı, ayarları ve istersen profili siler |

Kaldırmak için önce `forget`, sonra `claude mcp remove posta --scope user`.

## Kullanım

Claude'a normal cümlelerle yaz:

- "Son 7 günde gelen postalarımı listele."
- "Dekanlıktan gelen son postayı oku ve özetle."
- "Konusunda 'staj' geçen postaları bul."
- "Bu postaya kibar bir yanıt taslağı hazırla."
- "16 Ekim duyurularının tam metnini göster."

Hazır komutlar (sayı, kaç günlük postaya bakılacağı; boş bırakırsan 7):

| Komut | Ne yapar |
|---|---|
| `/posta:baslangic` | Bağlantıyı dener, neler yapabildiğini anlatır, ilgi alanlarını sorar |
| `/posta:hoca-ozet 7` | Hocalardan ve personelden gelen postaların özeti |
| `/posta:ogrenci-ozet 7` | Öğrencilerden gelen postaların özeti |
| `/posta:bana-ozet 3` | İlgi alanlarına (örneğin Güney Kampüs, yemekhane, tenis) göre okul duyuruları |

Claude Desktop'ta bu komutlar sohbet kutusundaki "+" menüsündedir. Özetten sonra istediğin postanın tam metnini isteyebilirsin. İlgi alanlarını Claude'a söylemen yeter ("profilime tenis ekle").

Claude'un kullandığı araçlar:

| Ne için | Araçlar |
|---|---|
| Okuma | `sunucu_bilgisi`, `klasorleri_listele`, `postalari_ara`, `postayi_oku`, `eki_oku` |
| Düzenleme | `postalari_isaretle`, `postalari_tasi`, `cope_at`, `arsivle` |
| Taslak | `taslak_olustur`, `yanit_taslagi_olustur` |
| Gönderme (kapalı) | `posta_gonder`, `taslagi_gonder` |
| Özet ve profil | `hoca_ozet`, `ogrenci_ozet`, `bana_ozet`, `profili_goster`, `profili_guncelle` |

## Güvenlik

- **Parolan** sadece işletim sisteminin anahtar zincirinde (Claude Desktop'ta Desktop'ın kendi güvenli deposunda) durur; hiçbir dosyaya ya da kayda yazılmaz.
- **Okumak postayı "okundu" yapmaz** ve **hiçbir şey kalıcı olarak silinmez** ("sil", Çöp kutusuna taşımak demektir).
- **Gönderme kapalı gelir.** Açılsa bile her posta, alıcıları ve metninin tamamı gösterilip sen onaylamadan gitmez. Claude Desktop bu onayı gösteremeyebilir; orada taslak hazırlatıp kendi posta uygulamandan gönder.
- **Posta içeriğine güvenilmez.** Postanın içindeki "şunu yap" gibi cümleler komut sayılmaz.
- **Ağa açılmaz.** Hiçbir port dinlemez; bağlantılar her zaman şifreli ve sertifikası doğrulanır.

Göndermeyi açmak istersen (sadece Claude Code): `config.toml` dosyasına `[sending]` altında `enabled = true` yaz ya da `claude mcp add` komutuna `--env POSTA_ALLOW_SEND=true` ekle. Dosyanın yerini `doctor` gösterir. Onayları otomatik kabul eden bir hook kurma.

## Sorun giderme

Claude bir hata aldığında sebebini ve ne yapacağını Türkçe söyler. Claude Code'da `doctor` komutu da her sorunun altına çözümünü yazar.

- **Giriş olmuyor:** Parolanın süresi dolmuş olabilir. `mail.bogazici.edu.tr/password` adresinden değiştir, sonra yeni parolayı gir (Desktop: eklenti ayarları; Claude Code: `parola`).
- **Bağlantı kurulamıyor:** İnternetini kontrol et; kampüs dışındaysan BU-VPN'i dene.
- **Araçlar görünmüyor:** Desktop'ta eklentinin açık olduğuna bak; Claude Code'da `/mcp` yaz.

Parolanı komut satırına ya da sohbete yazma.

## Boğaziçi ayarları

Sunucular e-posta adresinin uzantısından seçilir; kullanıcı adı `@` öncesi kısımdır (`ad.soyad`).

| Hesap | Adres | IMAP | SMTP |
|---|---|---|---|
| Öğrenci (doğrulandı) | `@std.bogazici.edu.tr` | `imap.std.bogazici.edu.tr:993` | `smtp.std.bogazici.edu.tr:587` |
| Personel (doğrulanmadı) | `@bogazici.edu.tr`, `@boun.edu.tr` | `imap.bogazici.edu.tr:993` | `smtp.bogazici.edu.tr:587` |

- `Archive` klasörü sunucuda yoktur; ilk arşivlemede oluşturulur.
- Yarı zamanlı (`pt.`) ve emekli (`retired.`) hesaplarda `POSTA_IMAP_HOST` ve `POSTA_SMTP_HOST` değişkenlerini kendin vermelisin.
- Hoca/öğrenci/liste ayrımı gönderen adresine göre tahmin edilir; kişisel adresinden yazan bir hoca "dış" posta sayılır.

## Uyarılar

- **Resmi değildir.** Boğaziçi Üniversitesi'yle bağlantısı yoktur ve onun tarafından onaylanmamıştır. Üniversite hesabını bağlamadan önce BT ve kabul edilebilir kullanım kurallarına bak.
- Okuduğun postalar, kullandığın uygulamanın yapay zekâ sağlayıcısına (Claude için Anthropic'e) gönderilir.
- Yapay zekâ postayı yanlış okuyabilir ya da yanlış özetleyebilir. Son tarihleri, notları ve sınav tarihlerini asıl postadan kontrol et.
- Gönderme varsayılan olarak kapalıdır ve her zaman senin onayını ister. Gönderdiğin her şeyden sen sorumlusun.
- `unverified` (henüz doğrulanmadı) diye işaretli ayarlar gerçek sunucuda denenmedi; `doctor` listeler.
- Yazılım olduğu gibi sunulur, hiçbir garanti verilmez. Lisans: MIT ([LICENSE](LICENSE)).

## Geliştiriciler için

```
git clone https://github.com/kutluege/bumail_mcp
cd bumail_mcp
uv sync
uv run pytest tests/unit -q
uv run posta-mcp setup      # Claude Code'a bu klasörü kullanan komutla ekler
```

Testler: [docs/TESTING.md](docs/TESTING.md) · Tasarım: [docs/SPEC.md](docs/SPEC.md) · İstemciler: [docs/CLIENTS.md](docs/CLIENTS.md) · Sürüm ve doğrulama: [docs/HANDOFF.md](docs/HANDOFF.md)
