import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Use Windows Native Certificate Store for Python SSL requests
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# Load environment variables from local .env file
load_dotenv(override=True)


class Config:
    """Nutanix Prism Central & LLM Configuration Loader."""

    # Nutanix Prism Central
    PC_HOST: str = os.getenv("PC_HOST", "").strip()
    PC_PORT: str = os.getenv("PC_PORT", "9440").strip()
    PC_USERNAME: str = os.getenv("PC_USERNAME", "").strip()
    PC_PASSWORD: str = os.getenv("PC_PASSWORD", "").strip()
    PC_INSECURE: str = os.getenv("PC_INSECURE", "true").strip().lower()
    READ_ONLY_MODE: str = os.getenv("READ_ONLY_MODE", "false").strip().lower()
    BASTION_VM_NAME: str = os.getenv("BASTION_VM_NAME", "LinuxTools").strip()
    BASTION_VM_IP: str = os.getenv("BASTION_VM_IP", "20.20.20.14").strip()
    WINDOWS_VM_NAME: str = os.getenv("WINDOWS_VM_NAME", "Windows2022-VM").strip()
    WINDOWS_VM_IMAGE: str = os.getenv("WINDOWS_VM_IMAGE", "Windows 2022").strip()
    WINDOWS_VM_CONTAINER: str = os.getenv("WINDOWS_VM_CONTAINER", "nkp").strip()
    WINDOWS_VM_VCPU: int = int(os.getenv("WINDOWS_VM_VCPU", "8"))
    WINDOWS_VM_MEMORY_GB: int = int(os.getenv("WINDOWS_VM_MEMORY_GB", "10"))
    WINDOWS_VM_DISK_GB: int = int(os.getenv("WINDOWS_VM_DISK_GB", "110"))
    WINDOWS_VM_IP: str = os.getenv("WINDOWS_VM_IP", "20.20.20.17").strip()
    WINDOWS_VM_GATEWAY: str = os.getenv("WINDOWS_VM_GATEWAY", "20.20.20.1").strip()
    WINDOWS_VM_PROJECT: str = os.getenv("WINDOWS_VM_PROJECT", "default").strip()

    # LLM & Tracing Settings
    MODEL_PROVIDER: str = os.getenv("MODEL_PROVIDER", "google").strip().lower()
    MODEL_NAME: str = os.getenv("MODEL_NAME", "gemini-2.5-flash").strip()
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
    GOOGLE_API_KEY: str = (os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()

    # LangSmith Tracing
    LANGSMITH_TRACING: str = os.getenv("LANGSMITH_TRACING", "false").strip().lower()
    LANGSMITH_API_KEY: str = os.getenv("LANGSMITH_API_KEY", "").strip()
    LANGSMITH_PROJECT: str = os.getenv("LANGSMITH_PROJECT", "nutanix-prism-mcp-agent").strip()

    # Project paths
    PROJECT_ROOT: Path = Path(__file__).parent.resolve()
    SERVER_DIR: Path = PROJECT_ROOT / "ntnx-api-mcp-server"

    @classmethod
    def get_server_command(cls) -> tuple[str, list[str]]:
        """Returns the command tuple (binary, args) to invoke the Nutanix MCP stdio server process.
        
        Prefers running via python module `ntnx_mcp` inside active virtual environment to ensure portability across systems.
        """
        python_exec = sys.executable
        return python_exec, ["-m", "ntnx_mcp", "serve_stdio"]

    @classmethod
    def get_server_env(cls) -> dict[str, str]:
        """Builds environment variables dictionary passed to the Nutanix MCP server process."""
        env = {
            "PC_HOST": cls.PC_HOST,
            "PC_PORT": cls.PC_PORT,
            "PC_USERNAME": cls.PC_USERNAME,
            "PC_PASSWORD": cls.PC_PASSWORD,
            "PC_INSECURE": cls.PC_INSECURE,
            "READ_ONLY_MODE": cls.READ_ONLY_MODE,
            "PYTHONPATH": str(cls.SERVER_DIR / "src"),
        }
        # Inherit PATH and SYSTEMROOT for Windows subprocess compatibility
        for key in ("PATH", "SYSTEMROOT", "PATHEXT", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    @classmethod
    def validate(cls) -> None:
        """Validates that mandatory Prism Central environment variables are supplied."""
        missing = []
        if not cls.PC_HOST:
            missing.append("PC_HOST")
        if not cls.PC_USERNAME and not os.getenv("PC_API_KEY"):
            missing.append("PC_USERNAME / PC_API_KEY")
        if missing:
            raise ValueError(
                f"Missing required environment variables for Nutanix Prism Central: {', '.join(missing)}. "
                "Please configure them in your .env file."
            )


# Export LangChain & LangSmith environment variables globally for tracing (NO hardcoded secrets)
if Config.LANGSMITH_API_KEY:
    os.environ["LANGCHAIN_TRACING_V2"] = Config.LANGSMITH_TRACING
    os.environ["LANGSMITH_TRACING"] = Config.LANGSMITH_TRACING
    os.environ["LANGCHAIN_API_KEY"] = Config.LANGSMITH_API_KEY
    os.environ["LANGSMITH_API_KEY"] = Config.LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = Config.LANGSMITH_PROJECT
    os.environ["LANGSMITH_PROJECT"] = Config.LANGSMITH_PROJECT
    os.environ["LANGSMITH_DANGEROUSLY_IGNORE_CERTS"] = "true"
    os.environ["PYTHONHTTPSVERIFY"] = "0"
