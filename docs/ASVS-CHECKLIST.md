# Target OWASP ASVS 5.0.0 Level 2 — subset awal

Rujukan rilis: [OWASP ASVS 5.0.0](https://github.com/OWASP/ASVS/releases/tag/v5.0.0). Daftar tematik ini belum pemetaan seluruh requirement ID ASVS. Tidak menyatakan sertifikasi atau kepatuhan penuh. `not applicable` hanya untuk kode checkpoint aktif; evaluasi ulang saat fitur ditambahkan.

| Area kontrol relevan | Applicability | Status | Bukti / kekurangan |
| --- | --- | --- | --- |
| Password hashing modern/salt | applicable | pass | PBKDF2 framework Django; bukan penyimpanan password PPP |
| Panjang/validasi sandi baru | applicable | pass | Validator framework minimal 12, reset form |
| MFA pemilik/privilege | applicable | pass lokal | TOTP wajib operator, secret Fernet, recovery sekali pakai, OTP replay parallel, enrollment/challenge browser; audit independen belum |
| Rate limit login/reset | applicable | pass terbatas | AuthRateBucket conditional counter, tes 429 dan race paralel; load test/edge produksi belum |
| Generic error / account recovery | applicable | pass terbatas | HTTP response generik, token acak framework, sekali pakai dan 30 menit; timing enumeration belum diuji |
| Session fixation/logout/reset invalidation | applicable | pass | Test rotasi key, POST logout, reset mencabut sesi |
| Idle/absolute session lifetime | applicable | pass | Middleware 30 menit/8 jam, test expiry |
| Secure/HttpOnly/SameSite cookies | applicable | pass konfigurasi / not tested HTTPS | HttpOnly/Lax; Secure dan SSL redirect saat DEBUG off, TLS deployment belum diuji |
| CSRF pada cookie auth | applicable | pass | Django middleware, POST tanpa token 403 |
| Deny-by-default action/object/tenant | applicable | pass subset | Tes role, IDOR lintas ISP, portal same-tenant customer, CSV/PDF/job |
| Field dan assignment scope | applicable | pass subset | Allowlist ModelForm, server price; Teknisi scope penugasan tersedia; sales/wilayah geografis belum tersedia |
| Privilege escalation / last owner | applicable | pass lokal | Owner-only editor, custom-cap allowlist/dependency, last-owner demotion/deactivation dan race, role changes revoke sessions |
| Validation/SQL injection/output encoding | applicable | pass subset | ORM parameterized, form limit/allowlist, markup footer test; fuzzing lengkap belum |
| PDF sandbox / resource fetch | applicable | pass desain + tes | ReportLab teks lokal; tidak ada file/URL fetch atau HTML browser |
| Upload/import validation | not applicable | not applicable | Tidak ada endpoint upload/impor |
| CSV formula injection | applicable | pass | Prefix sel berisiko + tenant/permission export tests |
| Payment webhook signature/freshness/order | not applicable | not applicable | Endpoint tidak tersedia, gateway belum diimplementasikan |
| Ledger constraints/idempotency/reversal | applicable | pass lokal | Unique period/event, integer rupiah, partial/reject overpay; single/bulk race, approved snapshots, stale/expiry, queue/resume/cancel dan concurrency tests |
| Job race / consistency | applicable | simulated | Recheck debt, retry/backoff, failure preserves payment; lab nyata belum |
| SSRF / router allowlist / network secrets | not applicable | not applicable | Tidak ada adapter network I/O atau URL-fetch publik |
| Production secret storage/rotation | applicable | not tested | Secret env + key lokal 0600; MFA key terpisah, Fernet encrypted TOTP; vault/rotation belum |
| Security headers/cache control | applicable | pass lokal | CSP/no-store/nosniff/frame DENY HTTP tests |
| HTTPS/HSTS/proxy deployment | applicable | not tested | Konfigurasi DEBUG-off tersedia, target deployment belum ada |
| Audit actor/tenant/action/correlation | applicable | pass subset | Domain mutations/CSV/jobs dicatat; login/factor failure/step-up/MFA/access events ditambahkan; password-failure/reset audit-event lengkap belum |
| Audit tamper resistance/retention/alarm | applicable | fail | Database admin bisa mengubah; retensi/alert belum |
| Encrypted backup / offsite / DR | applicable | fail sebagian | Restore/reconcile lokal pass; enkripsi/offsite/RPO/RTO belum |
| Dependency vulnerabilities | applicable | pass saat scan | Sebelas runtime package, pip-audit via PyPI; monitoring kontinu belum |
| Secret scanning | applicable | pass terbatas | Pemeriksaan diff/source manual, generated data/key diabaikan Git; scanner khusus belum |
| Production capacity/resource limits | applicable | not tested | SQLite/single-node development; load/concurrency multi-process belum |

Sumber bukti terulang: `core/tests/`, `scripts/browser_smoke.py`, `scripts/security_smoke.py`, `scripts/verify_pdf.py`, `scripts/verify_restore.py`. Detail perintah dan hasil ada di [TEST-REPORT](TEST-REPORT.md).

Checkpoint massal menambah 29 tes (95 total). Batas data preview, hak bulk_issue terpisah, konfirmasi MFA/CSRF, per-creator tenant scope dan recheck worker diverifikasi lokal; itu tidak memperluas klaim menjadi seluruh ASVS L2. Penjadwalan unattended, notifikasi, integrasi gateway/router dan multi-process database masih memerlukan kontrol tersendiri.

Checkpoint operasional menambah 34 tes (129 total) untuk scope penugasan, privacy komentar, state/version/replay, SLA snapshot dan quote aktivasi atomik/MFA. Bukti pada SUPPORT-AND-INSTALLATIONS.md. Tidak ada klaim bahwa upload, live router, gateway, PostgreSQL multi-process atau seluruh ASVS L2 telah selesai.
