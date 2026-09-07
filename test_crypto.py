"""Unit tests for Fernet encrypt/decrypt round-trip and failure modes."""

import unittest

from cryptography.fernet import Fernet

from app.crypto import decrypt_secret, encrypt_secret


class TestCrypto(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()

    def test_round_trip_recovers_the_original_value(self):
        token = encrypt_secret("minha-api-secret-super-longa", self.key)
        self.assertEqual(decrypt_secret(token, self.key), "minha-api-secret-super-longa")

    def test_encrypted_token_does_not_contain_the_plaintext(self):
        token = encrypt_secret("segredo-visivel-se-vazar", self.key)
        self.assertNotIn("segredo-visivel-se-vazar", token)

    def test_decrypt_with_the_wrong_key_raises(self):
        token = encrypt_secret("valor", self.key)
        wrong_key = Fernet.generate_key()
        with self.assertRaises(ValueError):
            decrypt_secret(token, wrong_key)

    def test_decrypt_garbage_token_raises(self):
        with self.assertRaises(ValueError):
            decrypt_secret("isso-nao-e-um-token-fernet-valido", self.key)

    def test_string_key_works_same_as_bytes_key(self):
        key_str = self.key.decode("utf-8")
        token = encrypt_secret("outro-valor", key_str)
        self.assertEqual(decrypt_secret(token, key_str), "outro-valor")


if __name__ == "__main__":
    unittest.main()
