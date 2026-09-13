from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy.hetzner.collect_operations_inventory import _image_priority, _repository


class OperationsInventoryScriptTests(SimpleTestCase):
    def test_repository_falls_back_to_compose_git_worktree(self):
        with TemporaryDirectory() as workdir:
            labels = {"com.docker.compose.project.working_dir": workdir}
            with patch(
                "deploy.hetzner.collect_operations_inventory._run",
                return_value="git@github.com:xmansx2030-lgtm/private-project.git",
            ) as run:
                repository = _repository(labels)

        self.assertEqual(repository, "xmansx2030-lgtm/private-project")
        run.assert_called_once_with(
            "git", "-C", workdir, "remote", "get-url", "origin", timeout=4
        )

    def test_application_image_wins_over_infrastructure_image(self):
        self.assertGreater(
            _image_priority(service="web", labels={}),
            _image_priority(service="postgres", labels={}),
        )

    def test_revision_label_has_highest_image_priority(self):
        labels = {"org.opencontainers.image.revision": "a" * 40}
        self.assertGreater(
            _image_priority(service="worker", labels=labels),
            _image_priority(service="web", labels={}),
        )
