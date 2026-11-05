package config
import("os";"strings")
type Config struct{ListenAddr,DBPath,LogLevel,FeishuVerificationToken,FeishuEncryptKey string;FeishuAllowedOpenIDs map[string]struct{}}
func Load()Config{return Config{ListenAddr:get("LIGHT_AGENT_LISTEN_ADDR","127.0.0.1:8080"),DBPath:get("LIGHT_AGENT_DB_PATH","./data/light-agent.db"),LogLevel:get("LIGHT_AGENT_LOG_LEVEL","info"),FeishuVerificationToken:os.Getenv("FEISHU_VERIFICATION_TOKEN"),FeishuEncryptKey:os.Getenv("FEISHU_ENCRYPT_KEY"),FeishuAllowedOpenIDs:set(os.Getenv("FEISHU_ALLOWED_OPEN_IDS"))}}
func get(k,d string)string{if v:=strings.TrimSpace(os.Getenv(k));v!=""{return v};return d}
func set(v string)map[string]struct{}{m:=map[string]struct{}{};for _,x:=range strings.Split(v,","){if x=strings.TrimSpace(x);x!=""{m[x]=struct{}{}}};return m}
