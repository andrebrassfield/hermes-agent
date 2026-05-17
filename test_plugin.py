import os
import sys
import importlib.util

def test_plugin():
    # Load module dynamically
    plugin_path = "plugins/obsidian-sync/plugin_api.py"
    spec = importlib.util.spec_from_file_location("plugin_api", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeCtx:
        def register_tool(self, **kwargs):
            print("Registering tool:", kwargs["name"])
            self.tool = kwargs["handler"]

    ctx = FakeCtx()
    module.register(ctx)

    # Create a dummy vault
    os.makedirs("test_vault/sub", exist_ok=True)
    with open("test_vault/test.md", "w") as f:
        f.write("This is a test note about obsidian.\nIt mentions Hermes.\n")
    with open("test_vault/sub/another.md", "w") as f:
        f.write("Another note, without the keyword.\n")

    print("Testing search...")
    res = ctx.tool(query="Hermes", vault_path="test_vault")
    print(res)

    # Assertions
    assert "test.md" in res, "Expected test.md in results"
    assert "Hermes" in res, "Expected 'Hermes' in output"
    print("\nAll assertions passed.")

if __name__ == "__main__":
    test_plugin()
