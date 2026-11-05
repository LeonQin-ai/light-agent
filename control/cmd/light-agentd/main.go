package main

import(
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/LeonQin-ai/light-agent/control/internal/config"
	"github.com/LeonQin-ai/light-agent/control/internal/dispatcher"
	"github.com/LeonQin-ai/light-agent/control/internal/feishu"
	"github.com/LeonQin-ai/light-agent/control/internal/httpapi"
	"github.com/LeonQin-ai/light-agent/control/internal/store"
)

func main(){
	cfg:=config.Load()
	logger:=slog.New(slog.NewJSONHandler(os.Stdout,&slog.HandlerOptions{Level:parseLevel(cfg.LogLevel)}))
	if !isLoopbackBind(cfg.ListenAddr)&&cfg.APIAuthToken==""{
		logger.Error("refusing non-loopback bind without LIGHT_AGENT_API_TOKEN","addr",cfg.ListenAddr)
		os.Exit(2)
	}
	st,err:=store.Open(cfg.DBPath);if err!=nil{logger.Error("open state store","error",err);os.Exit(1)}
	defer st.Close()
	ctx,stop:=signal.NotifyContext(context.Background(),syscall.SIGINT,syscall.SIGTERM);defer stop()
	go dispatcher.New(st,logger).Run(ctx)
	webhook:=feishu.New(st,feishu.Config{Token:cfg.FeishuVerificationToken,EncryptKey:cfg.FeishuEncryptKey,Allowed:cfg.FeishuAllowedOpenIDs})
	// Runtime endpoints remain fail-closed until a restricted C11 SuperAgent
	// driver is configured. User workloads are never executed by this process.
	server:=httpapi.HTTPServer(cfg.ListenAddr,httpapi.New(st,nil,webhook,cfg.APIAuthToken))
	go func(){logger.Info("light-agent control plane listening","addr",cfg.ListenAddr,"db",cfg.DBPath);if err:=server.ListenAndServe();err!=nil&&!errors.Is(err,http.ErrServerClosed){logger.Error("http server failed","error",err);stop()}}()
	<-ctx.Done()
	shutdownCtx,cancel:=context.WithTimeout(context.Background(),10*time.Second);defer cancel()
	if err:=server.Shutdown(shutdownCtx);err!=nil{logger.Error("http server shutdown","error",err)}
	logger.Info("light-agent stopped")
}

func isLoopbackBind(addr string)bool{
	host,_,err:=net.SplitHostPort(addr);if err!=nil{return false}
	if host=="localhost"{return true}
	ip:=net.ParseIP(strings.Trim(host,"[]"))
	return ip!=nil&&ip.IsLoopback()
}
func parseLevel(v string)slog.Level{switch v{case "debug":return slog.LevelDebug;case "warn":return slog.LevelWarn;case "error":return slog.LevelError;default:return slog.LevelInfo}}
