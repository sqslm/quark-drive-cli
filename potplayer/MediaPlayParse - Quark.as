/*
	夸克网盘 PlayParse 扩展（Quark Drive playback for PotPlayer）

	逻辑链接 https://quark.invalid/... 只携带文件标识，本扩展拦截后向本机
	回环桥接组件换取同目录选集或播放地址，播放器不会访问该域名。

	  .../folder/<fid>  整个目录的同目录自然排序播放列表
	  .../play/<fid>    从该视频开始的同目录选集
	  .../video/<fid>   播放列表内的单集条目：只按单集解析，不再展开成目录，
	                    避免每个条目反复触发 PlaylistParse。

	接口依据：PotPlayer Extension/api.txt 与自带 YouTube 扩展的函数签名，
	不复制其业务代码。

	MetaData 返回本地播放地址；每一集在真正播放时才换取地址，不预取全季直链。
	播放列表条目本身是稳定的文件标识，由组件负责自然排序与自动下一集。
*/

// bool PlayitemCheck(const string &in)						-> check playitem
// string PlayitemParse(const string &in, dictionary &, array<dictionary> &)	-> parse playitem
// bool PlaylistCheck(const string &in)						-> check playlist
// array<dictionary> PlaylistParse(const string &in)			-> parse playlist

string QUARK_VIDEO_PREFIX = "https://quark.invalid/video/";
string QUARK_PLAY_PREFIX = "https://quark.invalid/play/";
string QUARK_FOLDER_PREFIX = "https://quark.invalid/folder/";

// 扫码窗口由桥接组件打开；这里最多等待这段时间，扫码完成后继续当前操作。
int QUARK_LOGIN_WAIT_MS = 60000;

string GetTitle()
{
	// CP936 为简体中文界面，CP950 供繁体界面，其余回退英文。
	return "{$CP936=夸克网盘$}{$CP950=夸克網盤$}{$CP0=Quark Drive$}";
}

string GetVersion()
{
	return "0.8.0";
}

string GetDesc()
{
	return "https://pan.quark.cn/";
}

// -- 本机播放器进程 ---------------------------------------------------------

// 当前播放器（PotPlayer）的 PID；桥接用它把播放器与播放地址绑定以恢复进度。
// 由随包启动器 quark-potplayer.exe 的新本地动作 --player-pid 提供：启动器从自身
// 父进程链定位“真正拥有 PotPlayer 窗口”的进程并输出唯一 JSON 行；该动作不启动
// 桥接、不读取 Cookie、不访问网络。取不到、输出不可解析或非正整数时返回 0，
// PlayitemParse 据此省略字段，绝不猜测或回退到其他播放器。
// 每次解析按需调用一次，不缓存（多实例可能共享 INI），也不在此处发起额外请求。
int QuarkPlayerPid()
{
	string launcher = QuarkLauncher();

	if (launcher.empty()) return 0;

	string result = QuarkJsonObject(HostExecuteProgram(launcher, "--player-pid"));

	if (result.empty()) return 0;

	JsonReader reader;
	JsonValue json;

	if (!reader.parse(result, json) || !json.isObject()) return 0;

	JsonValue pid = json["player_pid"];

	if (!pid.isInt()) return 0;

	int value = pid.asInt();

	if (value <= 0) return 0;
	return value;
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
	return "扫码登录未完成，请重新打开该视频后重试";
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

// -- 解析 -------------------------------------------------------------------

array<dictionary> QuarkStatus(string message)
{
	array<dictionary> ret;
	dictionary row;

	row["title"] = message;
	ret.insertLast(row);
	return ret;
}

// 逻辑链接末尾的文件标识；含分隔符或查询符时视为无效。
string QuarkFid(const string &in path, const string &in prefix)
{
	if (path.find(prefix) != 0) return "";

	string fid = path.substr(prefix.length());

	if (fid.empty()) return "";
	if (fid.find("/") >= 0 || fid.find("\\") >= 0 || fid.find("\"") >= 0) return "";
	if (fid.find("?") >= 0 || fid.find("&") >= 0 || fid.find("#") >= 0) return "";
	return fid;
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

bool PlayitemCheck(const string &in path)
{
	return path.find(QUARK_VIDEO_PREFIX) == 0 || path.find(QUARK_PLAY_PREFIX) == 0;
}

string PlayitemParse(const string &in path, dictionary &MetaData, array<dictionary> &QualityList)
{
	string fid = QuarkFid(path, QUARK_PLAY_PREFIX);

	if (fid.empty()) fid = QuarkFid(path, QUARK_VIDEO_PREFIX);
	if (fid.empty()) return "";

	if (!QuarkEnsureLogin())
	{
		if (@MetaData !is null) MetaData["errorMessage"] = QuarkLoginReason();
		return "";
	}

	// 附带当前播放器 PID，桥接据此把播放器与播放地址绑定以恢复进度；
	// 取不到 PID（返回 0）时保持原来的仅 fid 请求体，不发送任何伪造字段。
	string body = "{\"fid\":\"" + fid + "\"}";
	int player_pid = QuarkPlayerPid();

	if (player_pid > 0) body = "{\"fid\":\"" + fid + "\",\"player_pid\":" + formatInt(player_pid) + "}";

	string response = QuarkRequest("/v1/play", body);
	string message;

	if (QuarkFailed(response, message))
	{
		if (@MetaData !is null) MetaData["errorMessage"] = message;
		return "";
	}

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return "";

	JsonValue url = root["url"];
	JsonValue title = root["title"];
	string base = HostLoadString("quark.base_url", "");

	if (!url.isString() || url.asString().empty() || base.empty()) return "";

	if (@MetaData !is null)
	{
		if (title.isString() && !title.asString().empty())
		{
			MetaData["title"] = title.asString();

			string ext = QuarkExtension(title.asString());

			if (!ext.empty()) MetaData["fileExt"] = ext.substr(1);
		}
		MetaData["webUrl"] = path;
	}
	return base + url.asString();
}

bool PlaylistCheck(const string &in path)
{
	// 只有目录和“从该集开始”的链接是播放列表；单集条目不再展开。
	return path.find(QUARK_FOLDER_PREFIX) == 0 || path.find(QUARK_PLAY_PREFIX) == 0;
}

array<dictionary> QuarkPlaylist(string request)
{
	array<dictionary> ret;
	string message;
	string response = QuarkRequest(request, "");

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的选集");

	JsonValue items = root["items"];

	if (!items.isArray()) return ret;

	for (int index = 0; index < items.size(); index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue fid = item["fid"];
		JsonValue name = item["name"];

		if (!fid.isString() || !name.isString() || fid.asString().empty()) continue;

		dictionary row;

		// 稳定条目：只带文件标识，播放该集时才换取地址。
		row["url"] = QUARK_VIDEO_PREFIX + fid.asString();
		row["title"] = name.asString();
		ret.insertLast(row);
	}
	return ret;
}

array<dictionary> PlaylistParse(const string &in path)
{
	string request;

	if (path.find(QUARK_FOLDER_PREFIX) == 0)
	{
		string parent = QuarkFid(path, QUARK_FOLDER_PREFIX);

		if (parent.empty()) return QuarkStatus("目录标识无效");
		request = "/v1/playlist?parent=" + HostUrlEncode(parent);
	}
	else if (path.find(QUARK_PLAY_PREFIX) == 0)
	{
		string start = QuarkFid(path, QUARK_PLAY_PREFIX);

		if (start.empty()) return QuarkStatus("视频标识无效");
		request = "/v1/playlist?start=" + HostUrlEncode(start);
	}
	else
	{
		return QuarkStatus("不支持的夸克网盘链接");
	}

	if (!QuarkEnsureLogin()) return QuarkStatus(QuarkLoginReason());

	return QuarkPlaylist(request);
}
