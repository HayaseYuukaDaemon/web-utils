package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"gopkg.in/yaml.v3"
)

const CNA_PATCH_FILE = "cna_patch.yaml"

type ProviderPatch struct {
	Proxies     yaml.Node `yaml:"proxies"`
	ProxyGroups yaml.Node `yaml:"proxy-groups"`
	Rules       yaml.Node `yaml:"rules"`
}

type ProxyApp struct {
	client *http.Client
	mux    *http.ServeMux
}

func readProviderPatch(filePath string) *ProviderPatch {
	file, err := os.Open(filePath)
	if err != nil {
		slog.Warn("Cannot read patch", "err", err)
		return nil
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	var patch ProviderPatch
	if err := decoder.Decode(&patch); err != nil {
		slog.Warn("Cannot decode patch", "err", err)
		return nil
	}
	return &patch
}

func loadCIAAccount() (string, string) {
	return os.Getenv("CIA_ACCOUNT"), os.Getenv("CIA_PASSWORD")
}

type NamedItem struct {
	Name string `yaml:"name"`
}

// patch不可复用
func ProcessCIAProxy(config *yaml.Node, patch *ProviderPatch) ([]byte, error) {
	proxyGroupWhiteList := []string{"\U0001F680 节点选择", "\U0001F1E8\U0001F1F3 中国大陆", "\U0001F4AC Telegram", "\U0001F916 OpenAI"}

	wrappedYAML := WrapYAML(config.Content[0])

	// 移除多余的规则组
	proxyGroups := wrappedYAML.GetMapValue("proxy-groups")
	diminishedProxyGroupNames := map[string]struct{}{}
	foreignProxyNames := []string{}
	if proxyGroups == nil {
		return nil, fmt.Errorf("未检测到代理组")
	}
	if err := proxyGroups.DeleteListIf(func(i int, item *EditableYAML) bool {
		if item == nil {
			return false
		}
		var pg NamedItem
		if err := item.Decode(&pg); err != nil {
			return false
		}
		// 过滤节点选择中的大陆节点
		if pg.Name == proxyGroupWhiteList[0] {
			selectProxies := item.GetMapValue("proxies")
			inForeignRegion := false
			if err := selectProxies.DeleteListIf(func(i int, item *EditableYAML) bool {
				if inForeignRegion {
					foreignProxyNames = append(foreignProxyNames, item.Value)
					return false
				}
				if strings.Contains(item.Value, "国际") {
					inForeignRegion = true
				}
				return true
			}); err != nil {
				slog.Warn("节点选择的proxies迭代失败", "name", pg.Name, "err", err)
				return false
			}
		}
		diminish := !slices.Contains(proxyGroupWhiteList, pg.Name)
		if diminish {
			diminishedProxyGroupNames[pg.Name] = struct{}{}
		}
		return diminish
	}); err != nil {
		return nil, fmt.Errorf("failed to delete proxy groups: %w", err)
	}

	// 过滤已无对应规则组的规则
	rules := wrappedYAML.GetMapValue("rules")
	if rules == nil {
		return nil, fmt.Errorf("无规则组")
	}
	if err := rules.DeleteListIf(func(i int, item *EditableYAML) bool {
		splitedRule := strings.Split(item.Value, ",")
		pgName := splitedRule[len(splitedRule)-1]
		if pgName == "no-resolve" {
			pgName = splitedRule[len(splitedRule)-2]
		}
		_, ok := diminishedProxyGroupNames[pgName]
		return ok
	}); err != nil {
		return nil, fmt.Errorf("failed to delete rules: %w", err)
	}

	// 处理patch
	if patch != nil {
		// 构造 patch 代理的 names
		patchProxyNames := []string{}
		if err := WrapYAML(&patch.Proxies).IterateList(func(i int, item *EditableYAML) error {
			var proxy NamedItem
			if err := item.Decode(&proxy); err != nil {
				return fmt.Errorf("failed to decode proxy: %w", err)
			}
			patchProxyNames = append(patchProxyNames, proxy.Name)
			return nil
		}); err != nil {
			return nil, fmt.Errorf("failed to iterate patch proxies: %w", err)
		}
		var patchProxyNamesYAML yaml.Node
		if err := patchProxyNamesYAML.Encode(patchProxyNames); err != nil {
			return nil, fmt.Errorf("failed to encode patch proxy names: %w", err)
		}
		// 为原有代理组添加 patch 代理
		if err := proxyGroups.IterateList(func(i int, item *EditableYAML) error {
			pgProxies := item.GetMapValue("proxies")
			if pgProxies == nil {
				slog.Warn("proxy group has no proxies", "index", i)
				return nil
			}
			pgProxies.Content = slices.Concat(pgProxies.Content, patchProxyNamesYAML.Content)
			return nil
		}); err != nil {
			return nil, fmt.Errorf("failed to iterate proxy groups: %w", err)
		}

		// 构造用于填充空代理组的 names
		var fillingProxyNamesYAML yaml.Node
		if err := fillingProxyNamesYAML.Encode(foreignProxyNames); err != nil {
			return nil, fmt.Errorf("failed to encode foreign proxy names: %w", err)
		}
		fillingProxyNamesYAML.Content = slices.Concat(fillingProxyNamesYAML.Content, patchProxyNamesYAML.Content)
		fillingProxyNamesYAML.Content = append(fillingProxyNamesYAML.Content, &yaml.Node{
			Kind:  yaml.ScalarNode,
			Value: "DIRECT",
		})
		// 填充空代理
		if err := WrapYAML(&patch.ProxyGroups).IterateList(func(i int, item *EditableYAML) error {
			proxies := item.GetMapValue("proxies")
			if proxies != nil {
				return nil
			}
			fillMe := item.GetMapValue("fill-me")
			if fillMe == nil {
				return nil
			}
			var enable bool
			if err := fillMe.Decode(&enable); err != nil {
				slog.Warn("invalid fill-me config", "err", err)
				return nil // 忽略错误
			}
			if !enable {
				return nil
			}
			item.DeleteMapValue("include-all")
			item.DeleteMapValue("fill-me")
			fillMe = nil
			item.AddMapValue("proxies", &fillingProxyNamesYAML)
			return nil
		}); err != nil {
			return nil, fmt.Errorf("failed to iterate patch proxy groups: %w", err)
		}

		// 添加代理
		proxies := wrappedYAML.GetMapValue("proxies")
		if proxies == nil {
			return nil, fmt.Errorf("你妈的笑话, 没代理你用尼玛呢")
		}

		// 添加代理
		proxies.Content = slices.Concat(proxies.Content, patch.Proxies.Content)
		// 添加代理组
		proxyGroups.Content = slices.Concat(proxyGroups.Content, patch.ProxyGroups.Content)
		// 添加规则
		rules.Content = slices.Insert(rules.Content, 0, patch.Rules.Content...)
	}

	final := bytes.NewBuffer([]byte{})
	if err := yaml.NewEncoder(final).Encode(config); err != nil {
		return nil, fmt.Errorf("failed to encode final config: %w", err)
	}
	return final.Bytes(), nil
}

func (a *ProxyApp) fetchSub(ctx context.Context, subURL string) (*yaml.Node, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", subURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "clash")
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("failed to fetch subscription: %d", resp.StatusCode)
	}
	var sub yaml.Node
	buf := bytes.NewBuffer([]byte{})
	_, err = io.Copy(buf, resp.Body)
	if err != nil {
		return nil, err
	}
	if err := yaml.NewDecoder(buf).Decode(&sub); err != nil {
		slog.Warn("resp", "body", buf.String())
		return nil, err
	}
	return &sub, nil
}

func (a *ProxyApp) fetchSubURL(ctx context.Context, jwt string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", "https://nmsl.cool/public/9e23c1958578/user/getSubscribe", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "clash")
	req.Header.Set("Authorization", jwt)
	resp, err := a.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return "", fmt.Errorf("failed to fetch subscription: %d", resp.StatusCode)
	}
	var serverResp struct {
		Data struct {
			SubscribeURL string `json:"subscribe_url"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&serverResp); err != nil {
		return "", err
	}
	if serverResp.Data.SubscribeURL == "" {
		return "", fmt.Errorf("no subscription URL found")
	}
	return serverResp.Data.SubscribeURL, nil
}

func (a *ProxyApp) getJWTToken(ctx context.Context, account, password string) (string, error) {
	url, err := url.Parse("https://nmsl.cool/public/9e23c1958578/passport/auth/login/")
	if err != nil {
		return "", err
	}
	q := url.Query()
	q.Add("email", account)
	q.Add("password", password)
	url.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, "POST", url.String(), nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "clash")
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return "", fmt.Errorf("failed to get JWT token: %d", resp.StatusCode)
	}
	var serverResp struct {
		Data struct {
			AuthData string `json:"auth_data"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&serverResp); err != nil {
		return "", err
	}
	if serverResp.Data.AuthData == "" {
		return "", fmt.Errorf("no JWT token found")
	}
	return serverResp.Data.AuthData, nil
}

func (a *ProxyApp) AppName() string {
	return "proxy"
}

func (a *ProxyApp) GetMux() *http.ServeMux {
	return a.mux
}

func (a *ProxyApp) RegisterMCPTools(m *mcp.Server) error {
	return nil
}

func NewProxyApp(client *http.Client) *ProxyApp {
	// 兼容CIA的抽象重定向
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) == 0 {
			return nil
		}
		prev := via[len(via)-1]
		if req.Response != nil && req.Response.StatusCode == 302 {
			req.Method = prev.Method
			// req.ContentLength = prev.ContentLength
			// req.GetBody = prev.GetBody
			// if prev.GetBody != nil {
			// 	body, err := prev.GetBody()
			// 	if err != nil {
			// 		return err
			// 	}
			// 	req.Body = body
			// }
		}
		return nil
	}

	app := &ProxyApp{
		client: client,
	}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /sub", func(w http.ResponseWriter, r *http.Request) {
		jwtFile, err := os.OpenFile("jwt_token", os.O_RDWR|os.O_CREATE, 0o600)
		if err != nil {
			http.Error(w, "打开jwt_token时出错: "+err.Error(), http.StatusInternalServerError)
			return
		}
		defer jwtFile.Close()
		jwtBytes, err := io.ReadAll(jwtFile)
		jwt := string(jwtBytes)
		if err != nil || len(jwt) == 0 {
			slog.Warn("读取jwt_token时出错或为空, 尝试更新JWT", "错误", err)
			account, password := loadCIAAccount()
			jwt, err = app.getJWTToken(r.Context(), account, password)
			if err != nil {
				http.Error(w, "获取JWT token时出错: "+err.Error(), 500)
				return
			}
			if _, err := jwtFile.WriteString(jwt); err != nil {
				http.Error(w, "写入JWT token时出错: "+err.Error(), http.StatusInternalServerError)
				return
			}
		}
		subUrl, err := app.fetchSubURL(r.Context(), jwt)
		if err != nil {
			slog.Warn("获取订阅URL时出错, 尝试刷新JWT", "错误", err)
			account, password := loadCIAAccount()
			jwt, err = app.getJWTToken(r.Context(), account, password)
			if err != nil {
				http.Error(w, "获取JWT token时出错: "+err.Error(), 500)
				return
			}
			if _, err := jwtFile.WriteString(jwt); err != nil {
				http.Error(w, "写入JWT token时出错: "+err.Error(), http.StatusInternalServerError)
				return
			}
			subUrl, err = app.fetchSubURL(r.Context(), jwt)
			if err != nil {
				http.Error(w, "刷新JWT后获取订阅URL再次出错: "+err.Error(), http.StatusInternalServerError)
				return
			}
		}
		sub, err := app.fetchSub(r.Context(), subUrl)
		if err != nil {
			http.Error(w, "获取订阅内容时出错: "+err.Error(), 500)
			return
		}
		if r.URL.Query().Get("raw") != "" {
			buf := bytes.NewBuffer([]byte{})
			if err := yaml.NewEncoder(buf).Encode(sub); err != nil {
				http.Error(w, "编码上游内容时出错: "+err.Error(), 500)
				return
			}
			w.Write(buf.Bytes())
		} else {
			patch := readProviderPatch("patch.yaml")
			patched, err := ProcessCIAProxy(sub, patch)
			if err != nil {
				http.Error(w, "处理上游内容时出错: "+err.Error(), 500)
				return
			}
			w.Write(patched)
		}
	})
	app.mux = mux
	slog.Info("Proxy app initialized")
	return app
}
