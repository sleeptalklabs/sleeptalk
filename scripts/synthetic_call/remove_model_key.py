"""Delete the model-key entry a "Just a model" run stored (run.sh calls this): uv run python remove_model_key.py <run_dir>"""
import sys
from pathlib import Path

from sleeptalk.credential_store import CredentialImpactError, CredentialStore

ref_file = Path(sys.argv[1]) / "model_ref.txt"
reference = ref_file.read_text().strip()
store = CredentialStore()
try:
    store.remove(reference, affected_profiles=[])
except CredentialImpactError as exc:
    # Only the run's throwaway profile uses it.
    store.remove(reference, affected_profiles=list(exc.profiles))
ref_file.unlink()
print("removed the run's model-key entry")
