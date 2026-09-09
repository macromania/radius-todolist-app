import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).parents[2] / "operations"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("publication", SCRIPTS / "publish-artifacts.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class RecipePublicationTests(unittest.TestCase):
    def test_existing_tag_without_trusted_record_is_rejected(self):
        self.exercise(trusted=False)

    def test_existing_tag_with_changed_digest_is_rejected(self):
        self.exercise(trusted=True)

    def exercise(self, trusted):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".state/azure"
            state.mkdir(parents=True)
            recipes = root / "infra/radius/recipes/azure"
            recipes.mkdir(parents=True)
            (recipes / "cluster.bicep").write_text("param context object\n")
            (recipes / "bicepconfig.json").write_text("{}")
            (root / "infra/bootstrap").mkdir()
            (state / "bootstrap.outputs.json").write_text(
                json.dumps(
                    {
                        "foundation": {
                            "registryName": "projectregistry",
                            "registryLoginServer": "projectregistry.azurecr.io",
                        },
                    }
                )
            )
            tag = "src-" + "a" * 20
            if trusted:
                (state / "recipes.json").write_text(
                    json.dumps(
                        {
                            "cluster": {
                                "reference": "projectregistry.azurecr.io/radius-recipes/"
                                f"cluster:{tag}",
                                "source_sha256": "a" * 64,
                                "digest": "sha256:" + "b" * 64,
                            },
                        }
                    )
                )
            commands = []

            def az(*args):
                commands.append(args)
                if args[:2] == ("acr", "login"):
                    return {"accessToken": "fake-token"}
                if args[:3] == ("acr", "repository", "list"):
                    return ["radius-recipes/cluster"]
                if args[:3] == ("acr", "repository", "show-tags"):
                    return [tag]
                if args[:3] == ("acr", "repository", "show"):
                    return {"digest": "sha256:" + "c" * 64}
                self.fail("Publication mutated an untrusted existing tag")

            digest = MagicMock()
            digest.hexdigest.return_value = "a" * 64
            with (
                patch.object(publication, "ROOT", root),
                patch.object(publication, "state_dir", return_value=state),
                patch.object(publication, "az", side_effect=az),
                patch.object(publication, "run", return_value="Bicep 0.42.1"),
                patch.object(publication.hashlib, "sha256", return_value=digest),
                patch.object(publication.subprocess, "run", return_value=MagicMock(returncode=0)),
            ):
                with self.assertRaises(publication.CommandError):
                    publication.main()
            self.assertFalse(any(args[:3] == ("acr", "repository", "update") for args in commands))


if __name__ == "__main__":
    unittest.main()
