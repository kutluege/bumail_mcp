<!-- mcp-name: io.github.kutluege/posta-mcp -->

# posta-mcp

English: [docs/README.en.md](docs/README.en.md)

## Bu nedir?

posta-mcp, Claude'un e-postanı okumasını, aramasını, düzenlemesini ve taslak yazmasını sağlayan küçük bir programdır. Kendi bilgisayarında çalışır ve doğrudan posta sunucusuna bağlanır. Aradaki başka bir servis yoktur.

Önce Boğaziçi Üniversitesi (bumail) için hazırlandı. İstersen başka bir IMAP/SMTP posta hesabıyla da kullanabilirsin (`generic` ayarı, sadece Claude Code ile).

**Hızlı başlangıç:** Claude Desktop kullanıyorsan `.mcpb` dosyasını indirip çift tıkla, e-posta ve parolanı gir. Ayrıntı: [Kurulum](#kurulum).

- **MCP**: Claude gibi yapay zekâ uygulamalarına yeni yetenekler ekleyen ortak bir standart. posta-mcp bu standartla çalışır.
- **IMAP / SMTP**: Posta kutusunu okumak (IMAP) ve posta göndermek (SMTP) için kullanılan olağan yöntemler.

> **Durum: alfa.** Öğrenci hesabı ayarları gerçek sunucuda denendi ve çalışıyor. Personel hesabı ayarları ve posta listesi kuralları henüz denenmedi. Ayrıntı için aşağıdaki [Boğaziçi ayarları](#boğaziçi-ayarları) bölümüne bak.

## Ne yapabilir?

- **Okuma ve arama:** Klasörleri listeler, postaları arar, bir postanın metnini ve eklerinin metnini okur.
- **Düzenleme:** Postayı okundu/okunmadı yapar, yıldızlar, başka klasöre taşır, arşivler, çöpe atar.
- **Taslak:** Yeni posta ya da yanıt taslağı hazırlar ve Taslaklar klasörüne koyar. Taslak gönderilmez.
- **Gönderme:** Varsayılan olarak kapalıdır. Açarsan bile her postayı sen onaylamadan gitmez (bkz. [Göndermeyi açmak](#göndermeyi-açmak)).
- **Türkçe özetler:** Üç hazır özet var. Claude özeti yazar, posta-mcp sadece uygun postaları seçip getirir.
  - `hoca-ozet`: Hocalardan ve personelden gelen son postaların özeti.
  - `ogrenci-ozet`: Öğrencilerden gelen son postaların özeti.
  - `bana-ozet`: Sana göre özet. İlgi alanlarını yazdığın küçük bir profil dosyasına bakar. İlgilendiğin duyuruları öne çıkarır, diğer okul postalarını kısa bir konu listesi olarak verir.

Profil örneği (`profile.toml`). Güney Kampüs'te okuyan, çoğu gün yemekhanede yemek yiyen ve tenis oynayan biri için:

```toml
notes = "Çoğunlukla Güney Kampüs'teyim; çoğu gün yemekhanede yerim; tenis oynarım."

[[interests]]
name = "Güney Kampüs"
keywords = ["güney kampüs", "south campus", "güney yerleşke"]

[[interests]]
name = "Yemekhane"
keywords = ["yemekhane", "cafeteria", "yemek listesi", "menü"]

[[interests]]
name = "Tenis"
keywords = ["tenis", "tennis", "tenis kortu", "kort"]
```

Her kelimenin tek halini yazman yeter. Büyük/küçük harf, Türkçe harfler ve ekler (kampüsünde, yemekhanede) otomatik anlaşılır. Profili elle düzenleyebilirsin ya da Claude'a "profilime tenis ekle" diyebilirsin. Dosyaya parola ya da özel bir şey yazma.

## Güvenlik

Bunlar kodda uygulanır ve testlerle denenir.

- **Parolan sadece işletim sisteminin anahtar zincirinde durur** (Windows Kimlik Bilgisi Yöneticisi, macOS Anahtar Zinciri gibi). Hiçbir dosyaya, kayda ya da çıktıya yazılmaz.
- **Okumak postayı "okundu" yapmaz.** Sen açıkça istemedikçe okunmamış postalar okunmamış kalır.
- **Hiçbir şey kalıcı olarak silinmez.** "Sil" demek "Çöp kutusuna taşı" demektir.
- **Gönderme varsayılan olarak kapalıdır.** Açsan bile her postada gönderenin, alıcıların, konunun ve metnin tamamı sana gösterilir ve onayını bekler. Onay istenemiyorsa gönderilmez, taslak önerilir.
- **Postanın içeriğine güvenilmez.** Posta metni "veri" olarak işaretlenir. Postanın içindeki "şunu yap" gibi cümleler komut sayılmaz.
- **Sadece senin bilgisayarında çalışır.** Ağda hiçbir port dinlemez. Bağlantılar her zaman şifreli (TLS) ve sertifikası doğrulanır. Doğrulamayı kapatan bir ayar yoktur.

## Kurulum

posta-mcp **Claude Desktop** ve **Claude Code** ile çalışır. claude.ai web sitesinde ve Claude mobil uygulamasında çalışmaz, çünkü program sadece senin bilgisayarında çalışır.

### Claude Desktop (en kolayı, terminal gerekmez)

1. [Sürümler sayfasından](https://github.com/kutluege/bogazici_postanesi/releases) `posta-mcp-<sürüm>.mcpb` dosyasını indir.
2. Dosyaya çift tıkla. Claude Desktop açılır ve eklentiyi kurmak isteyip istemediğini sorar. Açılmazsa Claude Desktop'ta Ayarlar > Uzantılar bölümüne dosyayı sürükle.
3. Açılan pencereye Boğaziçi e-posta adresini ve BUmail parolanı yaz. "E-posta göndermeye izin ver" kutusunu boş bırak.
4. Yeni bir sohbette `/posta:baslangic` yaz ya da "postalarımı kontrol et" de. Claude bağlantıyı dener ve neler yapabileceğini anlatır.

Parolanı daha sonra değiştirmek istersen (örneğin süresi dolduysa) Ayarlar > Uzantılar > Boğaziçi Postanesi bölümünden güncelle.

> Uzantı açılmazsa aşağıdaki "uv'yi kur" adımını yap ve Claude Desktop'u kapatıp yeniden aç.

Claude Desktop'ta gönderme büyük ihtimalle çalışmaz. Okuma, arama, düzenleme ve taslak çalışır. Taslaklar posta kutundaki Taslaklar klasörüne gider, oradan gönderebilirsin.

### Claude Code (iki komut)

**1. uv'yi kur.** uv, Python programlarını tek komutla çalıştıran küçük bir araçtır. Python'u ayrıca kurmana gerek kalmaz. Bir kez yapman yeter.

Windows (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS / Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Kurduktan sonra terminali kapatıp yeni bir terminal aç.

**2. Kurulumu başlat.**

```
uvx posta-mcp setup
```

Program sırayla:

- e-posta adresini sorar ve öğrenci ya da personel hesabı olduğunu kendisi anlar,
- parolanı sorar (yazarken görünmez),
- girişi dener; olmazsa sebebini Türkçe yazar ve parolayı yeniden sorar,
- sadece giriş başarılı olunca kaydeder (parola işletim sisteminin anahtar zincirine, gerisi bir ayar dosyasına),
- "Claude Code'a şimdi ekleyeyim mi?" diye sorar. "E" dersen kendisi ekler.

Sonra Claude Code'u yeniden başlat ve `/posta:baslangic` yaz.

> `uvx posta-mcp` komutları paket PyPI'de yayımlandıktan sonra çalışır. O zamana kadar şunu kullan:
> `uvx --from git+https://github.com/kutluege/bogazici_postanesi posta-mcp setup`

Claude Code'a elle eklemek istersen:

```
claude mcp add --scope user posta -- uvx posta-mcp serve
```

### Sık kullanılan komutlar

| Komut | Ne yapar |
|---|---|
| `uvx posta-mcp setup` | Hesabı kurar ya da baştan kurar |
| `uvx posta-mcp parola` | Sadece parolayı günceller (örneğin süresi dolduysa) |
| `uvx posta-mcp doctor` | Ayarları, parolayı ve girişi kontrol eder; sorun varsa ne yapacağını yazar |
| `uvx posta-mcp forget` | Bu bilgisayardaki hesap bilgilerini siler (parola, ayarlar, isteğe bağlı profil) |

### Güncelleme ve kaldırma

- **Claude Desktop:** Yeni sürümün `.mcpb` dosyasını indirip yeniden aç. Kaldırmak için Ayarlar > Uzantılar bölümünden eklentiyi kaldır.
- **Claude Code:** En yeni sürümü almak için `uvx posta-mcp@latest doctor` komutunu bir kez çalıştır. Kaldırmak için önce `uvx posta-mcp forget`, sonra `claude mcp remove posta --scope user`.

### Kaynaktan kurulum (geliştiriciler için)

```
git clone https://github.com/kutluege/bogazici_postanesi
cd bogazici_postanesi
uv sync
uv run posta-mcp setup
```

`setup`, Claude Code'a eklerken bu klasörü kullanan doğru komutu kendisi yazar.

## Kullanım

Claude'a normal cümlelerle yazabilirsin:

- "Son 7 günde gelen postalarımı listele."
- "Dekanlıktan gelen son postayı oku ve özetle."
- "Konusunda 'staj' geçen postaları bul."
- "Bu postaya kibar bir yanıt taslağı hazırla."
- "Okuduğum duyuru postalarını Arşiv'e taşı."

Hazır özetler Claude Code'da komut olarak çalışır. Sayı, kaç günlük postaya bakılacağıdır (boş bırakırsan 7):

```
/posta:baslangic
/posta:hoca-ozet 7
/posta:ogrenci-ozet 7
/posta:bana-ozet 3
```

`/posta:baslangic` bağlantıyı dener, neler yapabileceğini örneklerle anlatır ve ilgi alanlarını sorar; ilk kullanımda bununla başla. Claude Desktop'ta bu komutları sohbet kutusundaki "+" menüsünde bulursun; görmezsen Claude'a düz cümleyle "son 7 günün hoca postalarını özetle" de. Bu komutlar `/mcp__posta__hoca-ozet 7` biçiminde de yazılabilir. İstersen sayıdan sonra dil de yazabilirsin. Özetler varsayılan olarak Türkçe gelir.

Claude'un kullandığı araçlar:

| Ne için | Araçlar |
|---|---|
| Bilgi ve okuma | `server_info`, `list_folders`, `search_messages`, `get_message`, `get_attachment_text` |
| Düzenleme | `mark_messages`, `move_messages`, `trash_messages`, `archive_messages` |
| Taslak | `create_draft`, `create_reply_draft` |
| Gönderme (kapalı, onay gerekir) | `send_message`, `send_draft` |
| Özetler | `hoca_ozet`, `ogrenci_ozet`, `bana_ozet` |
| İlgi profili | `get_profile`, `update_profile` |

Bir aramada en fazla 100 sonuç gelir (varsayılan 20). Çok uzun posta metinleri 20.000 karakterde kesilir.

**Dosya eki:** `send_message`, `create_draft` ve `create_reply_draft` bilgisayarındaki dosyaları ekleyebilir (`attachments`: dosyaların tam yolu). En fazla 10 dosya ve toplam 18 MB eklenebilir. Gizli dosya ve klasörler (`.ssh`, `.env`, Windows'ta `AppData` gibi) ve ağ yolları (`\\sunucu\paylaşım`) hiç eklenmez. Gönderirken açılan onay penceresinde her ekin adı, boyutu ve bilgisayardaki yeri gösterilir.

## Göndermeyi açmak

Gönderme kapalı gelir. Açmak için aşağıdakilerden birini yap:

- Ortam değişkeni: `POSTA_ALLOW_SEND=true` (Claude Code'da `claude mcp add` komutuna `--env POSTA_ALLOW_SEND=true` ekle).
- Ayar dosyası `config.toml` içinde:

```toml
[sending]
enabled = true
```

`config.toml` dosyasının yerini `posta-mcp doctor` gösterir.

Açtıktan sonra bile `send_message` ve `send_draft` her seferinde bir onay penceresi gösterir: gönderen, alıcılar, Cc, Bcc, konu, ekler ve metnin tamamı. Sadece onayladığın posta gider. Uygulaman bu pencereyi gösteremiyorsa posta gönderilmez. O zaman taslak oluşturup kendi posta uygulamandan gönderebilirsin.

Dikkat: onayları otomatik kabul eden bir "hook" ayarlama. Bu, onay korumasını boşa çıkarır.

## Boğaziçi ayarları

`bogazici` ayarı, sunucuları e-posta adresinin uzantısına göre seçer:

| Hesap | Adres | IMAP (okuma, 993) | SMTP (gönderme, 587) |
|---|---|---|---|
| Öğrenci | `@std.bogazici.edu.tr` | `imap.std.bogazici.edu.tr` | `smtp.std.bogazici.edu.tr` |
| Personel / hoca | `@bogazici.edu.tr` (eski `@boun.edu.tr`) | `imap.bogazici.edu.tr` | `smtp.bogazici.edu.tr` |

**Kullanıcı adı**, e-posta adresinin `@` işaretinden önceki kısmıdır. Yani `ad.soyad@std.bogazici.edu.tr` için `ad.soyad`. `setup` bunu kendisi doldurur.

**Neler doğrulandı (13 Ekim 2026, öğrenci hesabıyla):**

- Öğrenci sunucuları: `imap.std.bogazici.edu.tr:993` (SSL) ve `smtp.std.bogazici.edu.tr:587` (STARTTLS). Giriş çalışıyor.
- Kullanıcı adı `@` öncesi kısımdır (`ad.soyad`).
- Klasörler: `Sent`, `Drafts`, `Trash`. `Archive` klasörü sunucuda yok; ilk arşivlemede otomatik oluşturulur.

**Henüz doğrulanmadı:** personel (`bogazici.edu.tr`) sunucuları, gönderen alan adları ve posta listesi kuralları (hoca/öğrenci ayrımı bunlara dayanır). Bunlar kod içinde `unverified` diye işaretli; `posta-mcp doctor` listeler.

Ayrıca:

- Yarı zamanlı (`pt.`) ve emekli (`retired.`) hesaplar başka sunucular kullanır. Bunlarda `POSTA_IMAP_HOST` ve `POSTA_SMTP_HOST` değişkenlerini kendin vermelisin.
- Üniversitenin 2026 Exchange geçişi bazı özellikleri değiştirmiş olabilir. Taşıma, arşivleme ve çöpe atma sunucu desteklemezse güvenlik için çalışmaz.
- Posta türleri (hoca, öğrenci, liste) tahmine dayanır. Kişisel adresinden yazan bir hoca "dış" posta sayılır.
- Doğrulama listesi: [docs/HANDOFF.md](docs/HANDOFF.md).

## Sorun giderme

Claude bir hata aldığında sebebini ve ne yapman gerektiğini Türkçe söyler. Claude Code kullanıyorsan şunu da çalıştırabilirsin:

```
uvx posta-mcp doctor
```

(Kaynaktan kurduysan `uv run posta-mcp doctor`.) Ayarları, anahtar zincirini ve girişi kontrol eder, her sorunun altına ne yapacağını yazar.

- **Giriş olmuyor:** Parolanın süresi dolmuş olabilir. `mail.bogazici.edu.tr/password` adresinden değiştir. Sonra Claude Desktop'ta Ayarlar > Uzantılar > Boğaziçi Postanesi bölümünden, Claude Code'da `uvx posta-mcp parola` komutuyla yeni parolayı gir. Kullanıcı adı olarak `ad.soyad` yerine tam adresi denemek için `POSTA_USERNAME` değişkenini kullanabilirsin.
- **Bağlantı kurulamıyor:** Kampüs dışındaysan ve üniversite IMAP için VPN istiyorsa önce BU-VPN'e bağlan. Bu da henüz doğrulanmadı.
- **Parola bulunamadı diyorsa:** Claude Desktop'ta parolayı eklenti ayarlarına gir; Claude Code'da `uvx posta-mcp setup` çalıştır.
- **Sunucu adı bulunamadı:** Adresin uzantısı tanınmıyor olabilir. `POSTA_IMAP_HOST` ve `POSTA_SMTP_HOST` değişkenlerini ver.
- **Claude'da araçlar görünmüyor:** Claude Desktop'ta Ayarlar > Uzantılar bölümünde eklentinin açık olduğuna bak. Claude Code'da `/mcp` yaz ya da terminalde `claude mcp list` çalıştır.
- **claude.ai web sitesinde ya da mobil uygulamada görünmüyor:** Normal; posta-mcp sadece Claude Desktop ve Claude Code ile çalışır.

Parolanı komut satırına ya da sohbete yazma. Hep `setup`, `parola` ya da eklenti ayarlarını kullan.

## Uyarılar

- **Resmi değildir.** Boğaziçi Üniversitesi'yle ya da başka bir kurumla bağlantısı yoktur ve bu kurumlar tarafından onaylanmamıştır.
- Üçüncü taraf bir aracı üniversite hesabına bağlamadan önce kurumunun BT ve kabul edilebilir kullanım kurallarına bak.
- Okuduğun posta içeriği, kullandığın uygulamanın yapay zekâ sağlayıcısına gönderilir (Claude kullanırsan Anthropic'e). Araçlarla okunan her şey bilgisayarından bu yolla çıkar.
- Yapay zekâ postayı yanlış okuyabilir ya da yanlış özetleyebilir. Son tarihleri, notları ve sınav tarihlerini mutlaka asıl postadan kontrol et.
- Gönderme varsayılan olarak kapalıdır ve her zaman senin onayını ister. Gönderdiğin her şeyden sen sorumlusun.
- `unverified` (doğrulanmadı) diye işaretli ayarlar gerçek sunucuda denenmedi.
- Yazılım olduğu gibi sunulur, hiçbir garanti verilmez. Lisans: MIT, bkz. [LICENSE](LICENSE).

## Geliştiriciler için

Testleri çalıştırmak için [docs/TESTING.md](docs/TESTING.md), tasarım için [docs/SPEC.md](docs/SPEC.md), sürüm çıkarma adımları için [docs/HANDOFF.md](docs/HANDOFF.md) dosyalarına bak. Hızlı kontrol:

```
uv sync
uv run pytest tests/unit -q
```
