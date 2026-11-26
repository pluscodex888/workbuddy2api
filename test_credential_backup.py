import pathlib
import secrets
import tempfile
import unittest

from credential_backup import credentials_to_export, decrypt_backup, encrypt_backup, restore_credentials


class CredentialBackupTests(unittest.TestCase):
    def test_encrypted_backup_round_trip_and_tamper_detection(self):
        entries = {"WB_TEST_TOKEN": secrets.token_urlsafe(32)}
        password = secrets.token_bytes(24)
        raw = encrypt_backup(entries, password)
        self.assertNotIn(entries["WB_TEST_TOKEN"].encode(), raw)
        self.assertEqual(decrypt_backup(raw, password), entries)
        with self.assertRaisesRegex(ValueError, "wrong backup passphrase or damaged"):
            decrypt_backup(raw[:-1] + bytes([raw[-1] ^ 1]), password)

    def test_export_selects_only_explicitly_declared_credentials(self):
        with tempfile.TemporaryDirectory() as base:
            config = pathlib.Path(base) / "scope.toml"
            config.write_text('[backup]\ncredentials = ["WB_TEST_TOKEN"]\n', encoding="utf-8")
            key = secrets.token_urlsafe(32)
            env = {"WB_TEST_TOKEN": key, "WB_GATEWAY_TOKEN": secrets.token_urlsafe(32)}
            result = credentials_to_export(config, ["WB_TEST_TOKEN"], env)
            self.assertEqual(result, {"WB_TEST_TOKEN": key})
            with self.assertRaisesRegex(ValueError, "explicitly declared"):
                credentials_to_export(config, ["WB_GATEWAY_TOKEN"], env)

    def test_restore_uses_a_new_directory_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as base:
            target = pathlib.Path(base) / "restored"
            secret_value = secrets.token_urlsafe(32)
            restore_credentials(target, {"WB_TEST_TOKEN": secret_value})
            restored = target / "WB_TEST_TOKEN.secret"
            self.assertEqual(restored.read_text(encoding="ascii").strip(), secret_value)
            with self.assertRaises(FileExistsError):
                restore_credentials(target, {"WB_TEST_TOKEN": secrets.token_urlsafe(32)})
            self.assertEqual(restored.read_text(encoding="ascii").strip(), secret_value)
