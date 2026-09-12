from unittest.mock import patch

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.test import SimpleTestCase

from reports.storage import R2MediaStorage, S3Boto3Storage, _compress_image_file


class ObjectStorageContractTests(SimpleTestCase):
    def test_non_image_upload_is_preserved_and_rewound(self):
        content = ContentFile(b"plain contract payload", name="contract.txt")
        content.read()

        compressed = _compress_image_file(content)

        self.assertIs(compressed, content)
        self.assertEqual(compressed.read(), b"plain contract payload")

    def test_r2_backend_delegates_upload_after_media_processing(self):
        self.assertIsNotNone(S3Boto3Storage)
        storage = R2MediaStorage(
            access_key="isolated-key",
            secret_key="isolated-secret",
            bucket_name="isolated-bucket",
            endpoint_url="https://isolated.invalid",
        )
        content = ContentFile(b"not an image", name="document.txt")

        with patch.object(
            S3Boto3Storage,
            "_save",
            autospec=True,
            return_value="school/document.txt",
        ) as save:
            name = storage._save("school/document.txt", content)

        self.assertEqual(name, "school/document.txt")
        delegated_content = save.call_args.args[2]
        self.assertEqual(delegated_content.read(), b"not an image")

    def test_r2_backend_rejects_parent_path_traversal(self):
        storage = R2MediaStorage(
            access_key="isolated-key",
            secret_key="isolated-secret",
            bucket_name="isolated-bucket",
            endpoint_url="https://isolated.invalid",
        )

        with self.assertRaises(SuspiciousFileOperation):
            storage.generate_filename("../outside.txt")
