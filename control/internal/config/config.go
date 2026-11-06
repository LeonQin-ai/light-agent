package config

import("os";"strings")

type Config struct{
	ListenAddr string
	DBPath string
	LogLevel string
	APIAuthToken string
	FeishuVerificationToken string
	FeishuEncryptKey string
	FeishuAllowedOpenIDs map[string]struct{}
	DockerSocket string
	WASMRuntime string
	SandboxManagerURL string
	SandboxManagerToken string
}

func Load()Config{
	return Config{
		ListenAddr:env("LIGHT_AGENT_LISTEN_ADDR","127.0.0.1:8080"),
		DBPath:env("LIGHT_AGENT_DB_PATH","./data/light-agent.db"),
		LogLevel:env("LIGHT_AGENT_LOG_LEVEL","info"),
		APIAuthToken:strings.TrimSpace(os.Getenv("LIGHT_AGENT_API_TOKEN")),
		FeishuVerificationToken:strings.TrimSpace(os.Getenv("FEISHU_VERIFICATION_TOKEN")),
		FeishuEncryptKey:strings.TrimSpace(os.Getenv("FEISHU_ENCRYPT_KEY")),
		FeishuAllowedOpenIDs:splitSet(os.Getenv("FEISHU_ALLOWED_OPEN_IDS")),
		DockerSocket:env("LIGHT_AGENT_DOCKER_SOCKET","/var/run/docker.sock"),
		WASMRuntime:env("LIGHT_AGENT_WASM_RUNTIME","wasmtime"),
		SandboxManagerURL:env("LIGHT_AGENT_SANDBOX_MANAGER_URL",""),
		SandboxManagerToken:strings.TrimSpace(os.Getenv("LIGHT_AGENT_SANDBOX_MANAGER_TOKEN")),
	}
}
func env(key,fallback string)string{if v:=strings.TrimSpace(os.Getenv(key));v!=""{return v};return fallback}
func splitSet(v string)map[string]struct{}{out:=map[string]struct{}{};for _,item:=range strings.Split(v,","){if item=strings.TrimSpace(item);item!=""{out[item]=struct{}{}}};return out}
