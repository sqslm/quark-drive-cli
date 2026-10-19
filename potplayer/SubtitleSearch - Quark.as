/*
	夸克网盘字幕搜索扩展（Quark Drive subtitle search for PotPlayer）

	在播放器原生的“字幕搜索”列表里列出当前夸克视频同目录的匹配 SRT/ASS，
	用户手动选择后由播放器加载。列表与字幕正文都来自本机回环桥接组件，
	播放器不会拿到夸克 Cookie 或签名地址。

	接口依据：PotPlayer Extension/api.txt 与自带的 SubtitleSearch - *.as 扩展
	导出（GetTitle/GetVersion/GetDesc/GetLanguages/SubtitleSearch/SubtitleDownload），
	不复制其业务代码，不查询任何第三方字幕网站，也不实现字幕上传。

	只处理当前夸克视频：
	  https://quark.invalid/play/<fid>            逻辑链接（从该集开始同目录选集）
	  https://quark.invalid/video/<fid>           播放列表内的单集条目
	  http://127.0.0.1:<port>/v1/stream/<session>/<name>   本机回环播放地址（当前会话）
	其它来源（本地文件、其它网络地址）返回空列表，不据此访问任意远端源。

	内嵌字幕与音轨保持播放器的原生解码与切换；本扩展只提供外挂候选，不自动选中、
	不替换内嵌轨、不改动全局字幕偏好，也不把字幕写盘。
*/

// string GetTitle()																-> get title for UI
// string GetVersion()																-> get version for manage
// string GetDesc()																	-> get detail information
// string GetLanguages()															-> get support language
// array<dictionary> SubtitleSearch(string MovieFileName, dictionary MovieMetaData)	-> search subtitle
// string SubtitleDownload(string id)												-> download subtitle

string QUARK_ACCOUNT_SCOPE = "";

string QUARK_VIDEO_PREFIX = "https://quark.invalid/video/";
string QUARK_PLAY_PREFIX = "https://quark.invalid/play/";

// 列表项 id 前缀：SubtitleDownload 只在形如 quark-subtitle-<fid> 的标识上工作。
string QUARK_SUBTITLE_ID_PREFIX = "quark-subtitle-";

// 扫码窗口由桥接组件打开；这里最多等待这段时间，扫码完成后继续搜索/下载。
int QUARK_LOGIN_WAIT_MS = 60000;

string GetTitle()
{
	// CP936 为简体中文界面，CP950 供繁体界面，其余回退英文。
	return "{$CP936=夸克网盘字幕$}{$CP950=夸克網盤字幕$}{$CP0=Quark Drive Subtitles$}";
}

string GetVersion()
{
	return "0.12.8";
}

string GetDesc()
{
	return "https://pan.quark.cn/";
}

string GetLanguages()
{
	// 字幕来自用户自己的网盘，语言随具体文件而定（zh/en/未知），搜索前无法声明；
	// 返回空表示不预过滤，逐条结果的语言按 lang 字段交给播放器。
	return "";
}

// -- 本地服务 ---------------------------------------------------------------

void QuarkClearService()
{
	HostSaveString("quark.base_url", "");
	HostSaveString("quark.token", "");
	HostSaveString("quark.instance", "");
}

// 健康返回实例标识；服务缺失、响应异常或实例不符时返回空串。
string QuarkHealth(string base)
{
	string body = HostUrlGetString(base + "/v1/health", "", "", "", true);

	if (body.empty()) return "";

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(body, root) || !root.isObject()) return "";

	JsonValue status = root["status"];
	JsonValue version = root["version"];
	JsonValue instance = root["instance"];

	if (!status.isString() || status.asString() != "ok") return "";
	if (!version.isString() || version.asString().empty()) return "";
	if (!instance.isString() || instance.asString().empty()) return "";

	return instance.asString();
}

// 启动器：安装器在 Extension 放置 quark-potplayer.exe（配套 quark-potplayer.path 记录组件路径），
// 所以优先找 exe；开发环境退回同目录的 quark-potplayer.cmd。两者都在扩展脚本上两级目录。
string QuarkLauncher()
{
	string folder = HostGetScriptFolder();

	while (!folder.empty() && (folder.Right(1) == "\\" || folder.Right(1) == "/"))
		folder = folder.Left(folder.length() - 1);

	// AngelScript 没有 C 风格数组；带长度的数组用 array<T> 的构造形式。
	array<string> roots(2);

	roots[0] = folder + "\\..\\..";
	roots[1] = HostGetExtensionFolder();

	for (int index = 0; index < 2; index++)
	{
		if (roots[index].empty()) continue;

		string exe = roots[index] + "\\quark-potplayer.exe";

		if (HostFileExist(exe)) return exe;

		string cmd = roots[index] + "\\quark-potplayer.cmd";

		if (HostFileExist(cmd)) return cmd;
	}
	return "";
}

// 取启动器输出中的唯一 JSON 对象，忽略可能的回车与多余行。
string QuarkJsonObject(string text)
{
	int start = text.find("{");

	if (start < 0) return "";

	text = text.substr(start);

	int end = text.find("}");

	if (end < 0) return "";

	return text.substr(0, end + 1);
}

// 保证本地服务可用：命中缓存的实例直接复用，否则重新拉起一次。
bool QuarkEnsureService()
{
	string base = HostLoadString("quark.base_url", "");
	string token = HostLoadString("quark.token", "");
	string instance = HostLoadString("quark.instance", "");

	if (!base.empty() && !token.empty())
	{
		string live = QuarkHealth(base);

		// 实例变化说明换了进程，缓存的令牌已失效；服务消失时同样要重启。
		if (!live.empty() && (instance.empty() || live == instance))
		{
			if (instance.empty()) HostSaveString("quark.instance", live);
			return true;
		}
		QuarkClearService();
	}

	string launcher = QuarkLauncher();

	if (launcher.empty()) return false;

	string result = QuarkJsonObject(HostExecuteProgram(launcher, "--ensure-service"));

	if (result.empty()) return false;

	JsonReader reader;
	JsonValue json;

	if (!reader.parse(result, json) || !json.isObject()) return false;

	JsonValue url = json["base_url"];
	JsonValue key = json["token"];

	if (!url.isString() || !key.isString()) return false;

	base = url.asString();
	token = key.asString();

	if (base.empty() || token.empty()) return false;

	HostSaveString("quark.base_url", base);
	HostSaveString("quark.token", token);

	string live = QuarkHealth(base);

	if (!live.empty()) HostSaveString("quark.instance", live);
	return true;
}

// 响应里错误对象的类型；非 JSON 或没有错误对象时返回空串。
string QuarkErrorType(string response)
{
	JsonReader reader;
	JsonValue root;

	if (response.empty() || !reader.parse(response, root) || !root.isObject()) return "";

	JsonValue error = root["error"];

	if (!error.isObject()) return "";

	JsonValue type = error["type"];

	if (type.isString()) return type.asString();
	return "";
}

// 带令牌的请求头；POST 一律要求关闭连接，避免未读请求体影响后续请求。
string QuarkHeader(string token, bool post)
{
	string header = "Authorization: Bearer " + token + "\r\n";

	if (post) header += "Connection: close\r\nContent-Type: application/json\r\n";
	return header;
}

// 控制接口调用：本地令牌失效或收到非 JSON 响应时重试一次。
string QuarkRequest(string path, string body)
{
	bool post = !body.empty();

	for (int attempt = 0; attempt < 2; attempt++)
	{
		if (!QuarkEnsureService()) return "";

		string base = HostLoadString("quark.base_url", "");
		string token = HostLoadString("quark.token", "");

		if (base.empty() || token.empty()) return "";

		string header = QuarkHeader(token, post);
		if (path.find("/v1/auth") != 0 && !QUARK_ACCOUNT_SCOPE.empty())
			header += "X-Quark-Account: " + QUARK_ACCOUNT_SCOPE + "\r\n";
		string response = HostUrlGetString(base + path, "", header, body, true);

		if (response.empty()) return "";

		string type = QuarkErrorType(response);

		if (type == "unauthorized")
		{
			QuarkClearService();
			continue;
		}
		if (type.empty())
		{
			JsonReader reader;
			JsonValue root;

			if (!reader.parse(response, root) || !root.isObject()) continue;
		}
		return response;
	}
	return "";
}

bool QuarkAuthenticated()
{
	string response = QuarkRequest("/v1/auth", "");

	if (response.empty()) return false;

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return false;

	JsonValue ok = root["authenticated"];

	return ok.isBool() && ok.asBool();
}

// /v1/auth 的登录状态（idle/waiting/success/failed）；无法解析或未带该字段时返回空串。
string QuarkLoginState()
{
	string response = QuarkRequest("/v1/auth", "");

	if (response.empty()) return "";

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return "";

	JsonValue state = root["login_state"];

	if (state.isString()) return state.asString();
	return "";
}

// 未登录时自动打开扫码窗口，并在有限时间内等待扫码结果。
bool QuarkEnsureLogin()
{
	if (QuarkAuthenticated()) return true;

	// 组件按需弹出扫码窗口；重复请求只返回当前状态。
	QuarkRequest("/v1/auth/login", "{\"client\":\"potplayer\"}");

	if (QuarkAuthenticated()) return true;

	int waited = 0;

	while (waited < QUARK_LOGIN_WAIT_MS)
	{
		HostSleep(1000);
		HostIncTimeOut(2000);
		waited += 1000;

		if (QuarkAuthenticated()) return true;

		// 窗口被关闭（idle）或本次登录失败（failed）说明扫码已结束，立即结束等待，
		// 不再空耗到超时；仅在 waiting 时继续等待手机确认。
		string state = QuarkLoginState();

		if (state == "idle" || state == "failed") return false;
	}
	return false;
}

// -- 当前视频识别 -----------------------------------------------------------

// 文件标识与会话标识只含安全字符：出现引号、路径分隔符或查询符时拒绝，
// 避免拼进请求 URL。
bool QuarkValidId(string value)
{
	if (value.empty()) return false;
	if (value.find("\"") >= 0 || value.find("\\") >= 0 || value.find("/") >= 0) return false;
	if (value.find("?") >= 0 || value.find("&") >= 0 || value.find("#") >= 0) return false;
	if (value.find(" ") >= 0 || value.find("\t") >= 0) return false;
	return true;
}

// 逻辑链接末尾的文件标识；取不到或含分隔符时返回空串。
string QuarkFid(const string &in path, const string &in prefix)
{
	if (path.find(prefix) != 0) return "";

	string fid = path.substr(prefix.length());
	int account = fid.find("?account=");
	if (account >= 0) fid = fid.Left(account);

	if (!QuarkValidId(fid)) return "";
	return fid;
}

// 本机回环播放地址 http://<loopback>:<port>/v1/stream/<session>/<name> 里的会话标识。
// 非回环主机或非流地址返回空串——绝不据此访问任意远端地址。
string QuarkStreamSession(const string &in source)
{
	if (source.find("http://") != 0) return "";

	string rest = source.substr(7);
	int slash = rest.find("/");

	if (slash <= 0) return "";

	string host = rest.substr(0, slash);
	string path = rest.substr(slash);
	string hostname = host;

	if (host.find("[") == 0)
	{
		int bracket = host.find("]");

		if (bracket > 0) hostname = host.substr(0, bracket + 1);
	}
	else
	{
		int colon = host.find(":");

		if (colon >= 0) hostname = host.substr(0, colon);
	}

	if (hostname != "127.0.0.1" && hostname != "localhost" && hostname != "[::1]") return "";
	if (path.find("/v1/stream/") != 0) return "";

	string session = path.substr(11);
	int separator = session.find("/");

	if (separator >= 0) session = session.substr(0, separator);

	if (!QuarkValidId(session)) return "";
	return session;
}

// 当前播放的夸克视频：优先逻辑链接，其次是本机回环播放地址；两者都不是时返回 false。
string QuarkAccountOf(const string &in path)
{
    int offset = path.find("?account=");
    if (offset < 0) return "";
    string value = path.substr(offset + 9);
    if (value.length() != 64) return "invalid";
    string hex = "0123456789abcdef";
    for (int index = 0; index < value.length(); index++)
        if (hex.find(value.substr(index, 1)) < 0) return "invalid";
    return value;
}

bool QuarkCurrentVideo(string source, string &out fid, string &out session)
{
	QUARK_ACCOUNT_SCOPE = QuarkAccountOf(source);
	fid = "";
	session = "";

	if (source.find(QUARK_PLAY_PREFIX) == 0)
	{
		fid = QuarkFid(source, QUARK_PLAY_PREFIX);
		return !fid.empty();
	}
	if (source.find(QUARK_VIDEO_PREFIX) == 0)
	{
		fid = QuarkFid(source, QUARK_VIDEO_PREFIX);
		return !fid.empty();
	}
	session = QuarkStreamSession(source);
	return !session.empty();
}

// -- 字幕搜索与下载 ---------------------------------------------------------

array<dictionary> SubtitleSearch(string MovieFileName, dictionary MovieMetaData)
{
	array<dictionary> ret;
	string fid;
	string session;
	string source = MovieFileName;

	// 只认当前夸克视频。普通本地文件或其它网络来源直接返回空列表，不触发登录、
	// 不访问任何远端源。
	if (!QuarkCurrentVideo(source, fid, session))
	{
		// MovieFileName 缺失时用播放器当前文件名补充识别，同样只接受夸克链接/回环地址。
		source = HostGetPlayingFileName();

		if (!QuarkCurrentVideo(source, fid, session)) return ret;
	}

	if (!QuarkEnsureLogin()) return ret;

	string request = "/v1/subtitles?session=" + HostUrlEncode(session);

	// 逻辑链接已知文件标识时用 fid；否则用当前会话。
	if (!fid.empty()) request = "/v1/subtitles?fid=" + HostUrlEncode(fid);

	string response = QuarkRequest(request, "");

	if (response.empty()) return ret;

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return ret;

	JsonValue account = root["account_id"];
	if (account.isString()) QUARK_ACCOUNT_SCOPE = account.asString();
	JsonValue items = root["items"];

	if (!items.isArray()) return ret;

	int count = items.size();

	for (int index = 0; index < count; index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue id = item["fid"];
		JsonValue name = item["name"];

		if (!id.isString() || !name.isString() || id.asString().empty()) continue;

		dictionary row;

		// id 前缀让 SubtitleDownload 能识别回本插件的列表项；title 用真实文件名。
		row["id"] = QUARK_SUBTITLE_ID_PREFIX + id.asString();
		if (!QUARK_ACCOUNT_SCOPE.empty()) row["id"] = QUARK_SUBTITLE_ID_PREFIX + id.asString() + "?account=" + QUARK_ACCOUNT_SCOPE;
		row["title"] = name.asString();

		JsonValue format = item["format"];

		if (format.isString() && !format.asString().empty()) row["format"] = format.asString();

		JsonValue lang = item["lang"];

		if (lang.isString() && !lang.asString().empty()) row["lang"] = lang.asString();

		ret.insertLast(row);
	}
	return ret;
}

string SubtitleDownload(string id)
{
	if (id.find(QUARK_SUBTITLE_ID_PREFIX) != 0) return "";

	QUARK_ACCOUNT_SCOPE = QuarkAccountOf(id);
	string fid = id.substr(QUARK_SUBTITLE_ID_PREFIX.length());
	int account = fid.find("?account=");
	if (account >= 0) fid = fid.Left(account);

	if (!QuarkValidId(fid)) return "";

	if (!QuarkEnsureLogin()) return "";

	// 字幕正文是 text/plain，不是 JSON。本地令牌失效时只刷新实例重试一次；
	// 任何错误响应（含错误 JSON）都返回空串，绝不把错误正文当作字幕交给播放器。
	for (int attempt = 0; attempt < 2; attempt++)
	{
		if (!QuarkEnsureService()) return "";

		string base = HostLoadString("quark.base_url", "");
		string token = HostLoadString("quark.token", "");

		if (base.empty() || token.empty()) return "";

		string header = QuarkHeader(token, false);
		if (!QUARK_ACCOUNT_SCOPE.empty()) header += "X-Quark-Account: " + QUARK_ACCOUNT_SCOPE + "\r\n";
		string text = HostUrlGetString(base + "/v1/subtitle?fid=" + HostUrlEncode(fid), "", header, "", true);

		if (text.empty()) return "";

		string type = QuarkErrorType(text);

		if (type.empty()) return text;

		if (type == "unauthorized")
		{
			QuarkClearService();
			continue;
		}
		return "";
	}
	return "";
}
