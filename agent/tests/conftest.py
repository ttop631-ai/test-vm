"""agent 테스트 공통 설정. app.main은 import 시점에 env로 Settings를 만들므로 토큰을 먼저 넣는다."""
import os

os.environ.setdefault("AGENT_TOKEN", "test-token")
