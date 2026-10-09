/*
	夸克网盘 UrlList 扩展（Quark Drive file browser for PotPlayer）

	目录浏览全部经本机回环桥接组件完成：播放器只拿到文件标识，不会拿到
	夸克 Cookie 或签名地址。组件由同目录启动器 quark-potplayer.exe / .cmd 拉起。

	接口依据：PotPlayer Extension/api.txt 与自带 YouTube 扩展的函数签名，
	不复制其业务代码。逻辑链接只含文件标识：
	  https://quark.invalid/folder/<fid>  目录（同目录自然排序播放列表）
	  https://quark.invalid/play/<fid>    从该视频开始的同目录选集
	  https://quark.invalid/video/<fid>   播放列表内的单集条目（不再展开）
*/

// string GetTitle() 																-> get title for UI
// string GetVersion()																-> get version for manage
// string GetDesc()																	-> get detail information
// array<dictionary> GetCategorys()													-> get category list
// array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)	-> get url list for Category

string QUARK_VIDEO_PREFIX = "https://quark.invalid/video/";
string QUARK_PLAY_PREFIX = "https://quark.invalid/play/";
string QUARK_FOLDER_PREFIX = "https://quark.invalid/folder/";

// 扫码窗口由桥接组件打开；这里最多等待这段时间，扫码完成后直接显示目录。
int QUARK_LOGIN_WAIT_MS = 60000;

// 与桥接组件 naming.VIDEO_EXTENSIONS 保持一致的播放列表扩展名。
string QUARK_VIDEO_EXTENSIONS = "|3gp|asf|avi|dat|divx|f4v|flv|iso|m2t|m2ts|m2v|m4v|mkv|mov|mp4|mpg|mpeg|mts|ogm|ogv|rm|rmvb|ts|vob|webm|wmv|";

string GetTitle()
{
	// CP936 为简体中文界面，CP950 供繁体界面，其余回退英文。
	return "{$CP936=夸克网盘$}{$CP950=夸克網盤$}{$CP0=Quark Drive$}";
}

string GetVersion()
{
	return "1";
}

string GetDesc()
{
	return "https://pan.quark.cn/";
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

		string response = HostUrlGetString(base + path, "", QuarkHeader(token, post), body, true);

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

bool QuarkFailed(string response, string &out message)
{
	if (response.empty())
	{
		message = "无法启动或连接本地网盘组件，请重新安装插件后重试";
		return true;
	}

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject())
	{
		message = "本地网盘组件返回了无法解析的响应";
		return true;
	}

	JsonValue error = root["error"];

	if (!error.isObject()) return false;

	JsonValue type = error["type"];
	JsonValue text = error["message"];

	if (type.isString() && type.asString() == "login_required")
	{
		message = "未登录夸克网盘，请在扫码窗口中完成扫码";
		return true;
	}
	if (text.isString() && !text.asString().empty()) message = text.asString();
	else message = "本地网盘组件请求失败";
	return true;
}

// -- 登录 -------------------------------------------------------------------

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

string QuarkLoginReason()
{
	string response = QuarkRequest("/v1/auth", "");

	if (!response.empty())
	{
		JsonReader reader;
		JsonValue root;

		if (reader.parse(response, root) && root.isObject())
		{
			JsonValue text = root["login_message"];

			if (text.isString() && !text.asString().empty()) return text.asString();
		}
	}
	return "扫码登录未完成，请刷新目录后重试";
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

// -- 列表 -------------------------------------------------------------------

array<dictionary> QuarkStatus(string message)
{
	array<dictionary> ret;
	dictionary row;

	row["title"] = message;
	ret.insertLast(row);
	return ret;
}

// 最后一段扩展名（含点，小写）；没有扩展名时返回空串。
string QuarkExtension(string name)
{
	string lower = name.MakeLower();
	int last = -1;
	int index = lower.find(".");

	while (index >= 0)
	{
		last = index;
		index = lower.find(".", index + 1);
	}
	if (last <= 0) return "";
	return lower.substr(last);
}

bool QuarkIsVideo(string name)
{
	string ext = QuarkExtension(name);

	if (ext.empty()) return false;
	return QUARK_VIDEO_EXTENSIONS.find("|" + ext.substr(1) + "|") >= 0;
}

array<dictionary> GetCategorys()
{
	array<dictionary> ret;
	dictionary item;

	item["title"] = "{$CP936=我的网盘$}{$CP950=我的網盤$}{$CP0=My Drive$}";
	item["Category"] = "drive";
	ret.insertLast(item);
	return ret;
}

array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)
{
	array<dictionary> ret;
	string parent = PathToken;
	string page = PageToken;
	string message;

	if (parent.empty()) parent = "0";
	if (page.empty()) page = "1";

	if (!QuarkEnsureLogin()) return QuarkStatus(QuarkLoginReason());

	string response = QuarkRequest("/v1/files?parent=" + HostUrlEncode(parent) + "&page=" + HostUrlEncode(page), "");

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的目录");

	if (!PathToken.empty() && page == "1")
	{
		dictionary up;

		up["title"] = "..";
		up["folder"] = "parent";
		ret.insertLast(up);
	}

	JsonValue items = root["items"];

	if (!items.isArray()) return ret;

	JsonValue more = root["has_more"];
	JsonValue page_value = root["page"];
	bool has_more = more.isBool() && more.asBool();
	int next_page = page_value.isInt() ? page_value.asInt() + 1 : 0;
	int count = items.size();

	for (int index = 0; index < count; index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue fid = item["fid"];
		JsonValue name = item["name"];
		JsonValue directory = item["is_dir"];

		if (!fid.isString() || !name.isString() || fid.asString().empty()) continue;

		dictionary row;

		row["title"] = name.asString();

		if (directory.isBool() && directory.asBool())
		{
			row["url"] = QUARK_FOLDER_PREFIX + fid.asString();
			row["folder"] = "1";
			row["PathToken"] = fid.asString();
		}
		else if (QuarkIsVideo(name.asString()))
		{
			// 从该集开始播放同目录自然排序列表，选集与自动下一集由 PlayParse 完成。
			row["url"] = QUARK_PLAY_PREFIX + fid.asString();
		}
		else
		{
			// 非视频文件不进列表，否则会成为无法播放的选集条目。
			continue;
		}
		ret.insertLast(row);
	}
	// 本页可能全是被过滤掉的非视频文件：只要后端还有后页就保留 PageToken，本页没有
	// 任何可显示行时补一条不可播放的提示行承载它，否则后面含视频的页会整页丢失。
	if (has_more && next_page > 0)
	{
		if (ret.size() == 0)
		{
			dictionary hint;

			hint["title"] = "加载下一页…";
			ret.insertLast(hint);
		}
		ret[ret.size() - 1]["PageToken"] = formatInt(next_page);
	}
	return ret;
}
