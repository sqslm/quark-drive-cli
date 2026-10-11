/*
	夸克网盘 UrlList 扩展（Quark Drive file browser for PotPlayer）

	目录浏览全部经本机回环桥接组件完成：播放器只拿到文件标识，不会拿到
	夸克 Cookie 或签名地址。组件由同目录启动器 quark-potplayer.exe / .cmd 拉起。

	接口依据：PotPlayer Extension/api.txt 与自带 YouTube 扩展的函数签名，
	不复制其业务代码。逻辑链接只含文件标识：
	  https://quark.invalid/folder/<fid>  目录（同目录自然排序播放列表）
	  https://quark.invalid/play/<fid>    从该视频开始的同目录选集
	  https://quark.invalid/video/<fid>   播放列表内的单集条目（不再展开）

	入口分类：我的网盘、搜索（根页面在 Query 里输入关键词）、收藏目录、最近观看、
	继续观看。收藏/取消收藏/清空最近、剧集观看状态都是列表内的导航动作行，靠
	PathToken 进入幂等动作页，动作行不是媒体地址；目录收藏状态由桥接的本地收藏
	列表判断。目录首屏会读取该目录的观看状态，给出“继续观看”与“剧集观看状态”
	两行；观看状态只保存在桥接的本地历史里，不上传任何数据。
*/

// string GetTitle() 																-> get title for UI
// string GetVersion()																-> get version for manage
// string GetDesc()																	-> get detail information
// array<dictionary> GetCategorys()													-> get category list
// array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)	-> get url list for Category

string QUARK_VIDEO_PREFIX = "https://quark.invalid/video/";
string QUARK_PLAY_PREFIX = "https://quark.invalid/play/";
string QUARK_FOLDER_PREFIX = "https://quark.invalid/folder/";

// 动作页 PathToken：动作行把它交给列表，动作页再按前缀执行一次幂等请求。
string QUARK_ACTION_ADD = "quark-action-add-";
string QUARK_ACTION_DEL = "quark-action-del-";
string QUARK_ACTION_CLEAR_RECENT = "quark-action-clear-recent";
// 剧集观看状态：管理页与单集标记动作页；标记动作携带目标状态，刷新重放同一动作。
string QUARK_ACTION_SERIES = "quark-action-series-";
string QUARK_ACTION_WATCH = "quark-action-watch-";

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
	return "0.7.0";
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

// 收藏动作、清空动作的目录标识来自本脚本生成的 PathToken；含引号、路径分隔符或
// 动作记号分隔符“|”时拒绝，避免拼进 JSON 请求体或破坏动作参数。
bool QuarkValidFid(string fid)
{
	if (fid.empty()) return false;
	if (fid.find("\"") >= 0 || fid.find("\\") >= 0 || fid.find("/") >= 0) return false;
	if (fid.find("|") >= 0) return false;
	if (fid.find("?") >= 0 || fid.find("&") >= 0 || fid.find("#") >= 0) return false;
	return true;
}

// 当前目录是否已在收藏里：读本地收藏列表，判断不出来时按未收藏处理。
bool QuarkIsFavorite(string fid)
{
	if (!QuarkValidFid(fid)) return false;

	string response = QuarkRequest("/v1/favorites", "");

	if (response.empty()) return false;

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return false;

	JsonValue items = root["items"];

	if (!items.isArray()) return false;

	int count = items.size();

	for (int index = 0; index < count; index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue value = item["fid"];

		if (value.isString() && value.asString() == fid) return true;
	}
	return false;
}

// 目录行：文件夹可点击进入，视频复用自然选集链接，其余文件跳过。标题在有 display
//（带观看进度的装饰文本）时用 display，但视频过滤仍按原始 name，扩展名判断与外挂
// 字幕匹配都不受装饰影响。收藏目录行会附带最近一集。
void QuarkAppendItems(array<dictionary> &ret, JsonValue &items)
{
	if (!items.isArray()) return;

	int count = items.size();

	for (int index = 0; index < count; index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue fid = item["fid"];
		JsonValue name = item["name"];
		JsonValue directory = item["is_dir"];
		JsonValue display = item["display"];

		if (!fid.isString() || !name.isString() || fid.asString().empty()) continue;

		dictionary row;

		if (display.isString() && !display.asString().empty()) row["title"] = display.asString();
		else row["title"] = name.asString();

		if (directory.isBool() && directory.asBool())
		{
			JsonValue last = item["last_episode"];

			if (last.isObject())
			{
				JsonValue last_name = last["name"];

				if (last_name.isString() && !last_name.asString().empty())
					row["title"] = name.asString() + " · 上次观看：" + last_name.asString();
			}
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
}

// 原生协议：PageToken 挂在最后一条行上。整页都被过滤掉时补一条提示行承载它，
// 否则后面含视频的页会整页丢失。
void QuarkAttachPageToken(array<dictionary> &ret, JsonValue &root)
{
	JsonValue more = root["has_more"];
	JsonValue value = root["page"];

	if (!more.isBool() || !more.asBool()) return;

	int next_page = value.isInt() ? value.asInt() + 1 : 0;

	if (next_page <= 0) return;

	if (ret.size() == 0)
	{
		dictionary hint;

		hint["title"] = "加载下一页…";
		ret.insertLast(hint);
	}
	ret[ret.size() - 1]["PageToken"] = formatInt(next_page);
}

// 收藏/取消收藏入口行：导航行，不是媒体地址；标题按已有状态取反，指向幂等动作页。
// 行标题不做 CP 选择展开——播放器只展开 GetTitle/GetCategorys 的元数据，
// 行标题里的 {$CP936=…$} 会原样显示，所以这里用纯文本。
void QuarkAppendFavoriteAction(array<dictionary> &ret, string fid, bool saved)
{
	dictionary row;

	if (saved)
	{
		row["title"] = "取消收藏当前目录";
		row["PathToken"] = QUARK_ACTION_DEL + fid;
	}
	else
	{
		row["title"] = "收藏当前目录";
		row["PathToken"] = QUARK_ACTION_ADD + fid;
	}
	row["folder"] = "1";
	ret.insertLast(row);
}

// 目录打不开（例如收藏已失效）时，只要本地收藏列表里还有这个目录，就保留取消入口。
array<dictionary> QuarkDirectoryFailure(string PathToken, string page, string message)
{
	array<dictionary> ret;

	if (!PathToken.empty() && page == "1" && QuarkIsFavorite(PathToken))
	{
		dictionary up;

		up["title"] = "..";
		up["folder"] = "parent";
		ret.insertLast(up);

		QuarkAppendFavoriteAction(ret, PathToken, true);
	}

	dictionary hint;

	hint["title"] = message;
	ret.insertLast(hint);
	return ret;
}

// 动作结果页：只有标题、没有地址或目录标记的行会被播放器过滤掉，所以不再单独发一条
// 结果提示行，而是把结果消息并入可导航的父级行标题，成功/失败与“返回”同时可见。
array<dictionary> QuarkActionResult(string message)
{
	array<dictionary> ret;
	dictionary back;

	back["title"] = message + " · 返回";
	back["folder"] = "parent";
	ret.insertLast(back);
	return ret;
}

// 单集观看状态动作记号：目标状态(1=已看/0=未看)、目录 fid、视频 fid 用“|”分隔。
// 三段都不含“|”（QuarkValidFid 拒绝），记号带目标状态，刷新重放同一动作即幂等。
string QuarkWatchToken(bool watched, string parent, string fid)
{
	string state = "0";

	if (watched) state = "1";
	return QUARK_ACTION_WATCH + state + "|" + parent + "|" + fid;
}

// 继续观看入口：本地历史里未看完的视频，按最近进度排序；条目标题使用带进度的装饰文本。
array<dictionary> QuarkContinueList()
{
	array<dictionary> ret;
	string response = QuarkRequest("/v1/continue", "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的继续观看列表");

	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("暂无可继续观看的视频：播放一段时间后会出现在这里");

	QuarkAppendItems(ret, items);
	return ret;
}

// 目录首屏的“继续观看”和“剧集观看状态”两行。只读取所选目录一次；读取失败时不
// 影响目录本身浏览，静默跳过这两行。全部看完时给一条可导航的完成状态行，避免播放器
// 过滤掉只有标题的行、也避免从头重播整季。
void QuarkAppendSeriesRows(array<dictionary> &ret, string parent)
{
	if (!QuarkValidFid(parent)) return;

	string response = QuarkRequest("/v1/series?parent=" + HostUrlEncode(parent), "");
	string message;

	if (QuarkFailed(response, message)) return;

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return;

	JsonValue total = root["total"];
	JsonValue watched = root["watched"];

	// 空目录或只有非视频文件时没有剧集：不显示继续/完成/管理行。
	if (total.isInt() && total.asInt() == 0) return;

	JsonValue target = root["continue"];

	if (target.isObject())
	{
		JsonValue fid = target["fid"];
		JsonValue title = target["display"];

		if (fid.isString() && QuarkValidFid(fid.asString()))
		{
			dictionary row;

			if (title.isString() && !title.asString().empty()) row["title"] = "继续观看：" + title.asString();
			else row["title"] = "继续观看";
			row["url"] = QUARK_PLAY_PREFIX + fid.asString();
			ret.insertLast(row);
		}
	}
	else
	{
		JsonValue complete = root["complete"];

		if (complete.isBool() && complete.asBool())
		{
			dictionary done;

			done["title"] = "本目录已全部看完 · 查看剧集状态";
			done["folder"] = "1";
			done["PathToken"] = QUARK_ACTION_SERIES + parent;
			ret.insertLast(done);
			return;
		}
	}

	string counts = "";

	if (total.isInt() && watched.isInt())
		counts = "（已看 " + formatInt(watched.asInt()) + "/" + formatInt(total.asInt()) + "）";

	dictionary manage;

	manage["title"] = "剧集观看状态" + counts;
	manage["folder"] = "1";
	manage["PathToken"] = QUARK_ACTION_SERIES + parent;
	ret.insertLast(manage);
}

// 浏览一个网盘目录：非根目录第一页插入“..”、收藏入口和剧集状态行，各出现一次。
array<dictionary> QuarkDirectoryList(string PathToken, string page, bool favorite_action)
{
	array<dictionary> ret;
	string parent = PathToken;

	if (parent.empty()) parent = "0";
	if (page.empty()) page = "1";

	string response = QuarkRequest("/v1/files?parent=" + HostUrlEncode(parent) + "&page=" + HostUrlEncode(page), "");
	string message;

	if (QuarkFailed(response, message)) return QuarkDirectoryFailure(PathToken, page, message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject())
		return QuarkDirectoryFailure(PathToken, page, "本地网盘组件返回了无法解析的目录");

	bool first = page == "1";

	if (!PathToken.empty() && first)
	{
		dictionary up;

		up["title"] = "..";
		up["folder"] = "parent";
		ret.insertLast(up);

		if (favorite_action) QuarkAppendFavoriteAction(ret, PathToken, QuarkIsFavorite(PathToken));

		QuarkAppendSeriesRows(ret, PathToken);
	}

	JsonValue items = root["items"];

	QuarkAppendItems(ret, items);
	QuarkAttachPageToken(ret, root);
	return ret;
}

// 收藏动作页：POST /v1/favorites 与 /v1/favorites/remove 都幂等，刷新重复执行无副作用。
array<dictionary> QuarkFavoriteActionPage(bool add, string fid)
{
	if (!QuarkValidFid(fid)) return QuarkActionResult("收藏操作缺少目录标识");

	string path = "/v1/favorites";

	if (!add) path = "/v1/favorites/remove";

	string response = QuarkRequest(path, "{\"fid\":\"" + fid + "\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);

	if (add) return QuarkActionResult("已收藏当前目录");
	return QuarkActionResult("已取消收藏当前目录");
}

// 清空最近记录：送一个非空 JSON 请求体（服务端忽略内容），重复执行幂等。
array<dictionary> QuarkRecentClearPage()
{
	string response = QuarkRequest("/v1/recent/clear", "{\"client\":\"potplayer\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkActionResult("已清空最近观看");
}

// 剧集观看状态管理页：只读取所选目录一次，逐集列出已看/未看；点击某集进入幂等的
// 标记动作页。未播放过的视频同样会出现，标记不依赖任何远端播放记录。
array<dictionary> QuarkSeriesPage(string parent)
{
	array<dictionary> ret;

	if (!QuarkValidFid(parent)) return QuarkActionResult("剧集状态缺少目录标识");

	string response = QuarkRequest("/v1/series?parent=" + HostUrlEncode(parent), "");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkActionResult("本地网盘组件返回了无法解析的剧集状态");

	JsonValue episodes = root["episodes"];

	if (!episodes.isArray() || episodes.size() == 0) return QuarkActionResult("该目录没有可管理的视频");

	dictionary up;

	up["title"] = "..";
	up["folder"] = "parent";
	ret.insertLast(up);

	for (int index = 0; index < episodes.size(); index++)
	{
		JsonValue episode = episodes[index];

		if (!episode.isObject()) continue;

		JsonValue fid = episode["fid"];
		JsonValue name = episode["name"];
		JsonValue watched = episode["watched"];

		if (!fid.isString() || !name.isString() || !QuarkValidFid(fid.asString())) continue;

		bool marked = watched.isBool() && watched.asBool();
		dictionary row;

		if (marked) row["title"] = name.asString() + " · 已看（点击标记未看）";
		else row["title"] = name.asString() + " · 未看（点击标记已看）";
		row["folder"] = "1";
		row["PathToken"] = QuarkWatchToken(!marked, parent, fid.asString());
		ret.insertLast(row);
	}
	return ret;
}

// 单集观看状态动作页：记号里带目标状态（1/0）、目录与视频 fid；重复刷新执行同一个
// 目标状态请求，因此幂等。视频的真实类型与大小由服务端目录清单校验。
array<dictionary> QuarkWatchActionPage(string spec)
{
	int first = spec.find("|");

	if (first < 0) return QuarkActionResult("观看状态操作缺少参数");

	int second = spec.find("|", first + 1);

	if (second < 0) return QuarkActionResult("观看状态操作缺少参数");

	string state = spec.substr(0, first);
	string parent = spec.substr(first + 1, second - first - 1);
	string fid = spec.substr(second + 1);

	if ((state != "0" && state != "1") || !QuarkValidFid(parent) || !QuarkValidFid(fid))
		return QuarkActionResult("观看状态操作参数无效");

	bool watched = state == "1";
	string flag = "false";

	if (watched) flag = "true";

	string body = "{\"fid\":\"" + fid + "\",\"parent\":\"" + parent + "\",\"watched\":" + flag + "}";
	string response = QuarkRequest("/v1/mark", body);
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);

	if (watched) return QuarkActionResult("已标记为已看");
	return QuarkActionResult("已标记为未看");
}

// 诊断入口：请本地服务拉起独立的“播放状态与诊断”窗口。窗口自行读取运行时记录，
// 脚本不接触令牌或地址；重复刷新时服务端复用同一个窗口，操作幂等。
array<dictionary> QuarkDiagnosticsPage()
{
	string response = QuarkRequest("/v1/diagnostics/open", "{\"client\":\"potplayer\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkActionResult("已打开播放状态与诊断窗口");
}

// 搜索根页面用 Query 查全网盘；进入结果目录后按 FID 浏览，不再使用 Query。
array<dictionary> QuarkSearchList(string Query, string PathToken, string page)
{
	if (!PathToken.empty()) return QuarkDirectoryList(PathToken, page, true);

	if (Query.empty()) return QuarkStatus("请在上方输入搜索关键词后回车");

	array<dictionary> ret;
	string response = QuarkRequest("/v1/search?q=" + HostUrlEncode(Query) + "&page=" + HostUrlEncode(page), "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的搜索结果");

	JsonValue items = root["items"];

	QuarkAppendItems(ret, items);
	QuarkAttachPageToken(ret, root);
	return ret;
}

// 收藏目录：根页面列出收藏，进入后按普通目录浏览。
array<dictionary> QuarkFavoritesList(string PathToken, string page)
{
	if (!PathToken.empty()) return QuarkDirectoryList(PathToken, page, true);

	array<dictionary> ret;
	string response = QuarkRequest("/v1/favorites", "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的收藏列表");

	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("收藏列表为空：进入目录后选“收藏当前目录”即可添加");

	QuarkAppendItems(ret, items);
	return ret;
}

// 最近观看：条目直接复用稳定播放链接，从该集开始同目录选集；根页面提供清空入口。
array<dictionary> QuarkRecentList()
{
	array<dictionary> ret;
	string response = QuarkRequest("/v1/recent", "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的最近记录");

	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("最近没有播放记录：打开一个视频后会出现在这里");

	dictionary clear;

	clear["title"] = "清空最近观看";
	clear["PathToken"] = QUARK_ACTION_CLEAR_RECENT;
	clear["folder"] = "1";
	ret.insertLast(clear);

	QuarkAppendItems(ret, items);
	return ret;
}

array<dictionary> GetCategorys()
{
	array<dictionary> ret;
	dictionary item;

	item["title"] = "{$CP936=我的网盘$}{$CP950=我的網盤$}{$CP0=My Drive$}";
	item["Category"] = "drive";
	ret.insertLast(item);

	dictionary keyword;

	keyword["title"] = "{$CP936=搜索网盘$}{$CP950=搜尋網盤$}{$CP0=Search Drive$}";
	keyword["Category"] = "search";
	// type=search 让播放器在根页面给出关键词输入框（Query）。
	keyword["type"] = "search";
	ret.insertLast(keyword);

	dictionary favorites;

	favorites["title"] = "{$CP936=收藏目录$}{$CP950=收藏目錄$}{$CP0=Favorite Folders$}";
	favorites["Category"] = "favorites";
	ret.insertLast(favorites);

	dictionary recent;

	recent["title"] = "{$CP936=最近观看$}{$CP950=最近觀看$}{$CP0=Recent Videos$}";
	recent["Category"] = "recent";
	ret.insertLast(recent);

	dictionary resume;

	resume["title"] = "{$CP936=继续观看$}{$CP950=繼續觀看$}{$CP0=Continue Watching$}";
	resume["Category"] = "continue";
	ret.insertLast(resume);

	dictionary diagnostics;

	diagnostics["title"] = "{$CP936=播放状态与诊断$}{$CP950=播放狀態與診斷$}{$CP0=Playback Status$}";
	diagnostics["Category"] = "diagnostics";
	ret.insertLast(diagnostics);
	return ret;
}

array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)
{
	string page = PageToken;

	if (page.empty()) page = "1";

	// 诊断窗口是纯本地功能：不需要夸克登录，必须在登录检查之前处理，
	// 否则未登录用户永远看不到诊断入口。
	if (Category == "diagnostics") return QuarkDiagnosticsPage();

	if (!QuarkEnsureLogin()) return QuarkStatus(QuarkLoginReason());

	// 动作页由列表里的动作行携带 PathToken 进入，刷新时重复执行同样的幂等请求。
	if (PathToken.find(QUARK_ACTION_ADD) == 0)
		return QuarkFavoriteActionPage(true, PathToken.substr(QUARK_ACTION_ADD.length()));

	if (PathToken.find(QUARK_ACTION_DEL) == 0)
		return QuarkFavoriteActionPage(false, PathToken.substr(QUARK_ACTION_DEL.length()));

	if (PathToken == QUARK_ACTION_CLEAR_RECENT) return QuarkRecentClearPage();

	if (PathToken.find(QUARK_ACTION_SERIES) == 0)
		return QuarkSeriesPage(PathToken.substr(QUARK_ACTION_SERIES.length()));

	if (PathToken.find(QUARK_ACTION_WATCH) == 0)
		return QuarkWatchActionPage(PathToken.substr(QUARK_ACTION_WATCH.length()));

	if (Category == "search") return QuarkSearchList(Query, PathToken, page);
	if (Category == "favorites") return QuarkFavoritesList(PathToken, page);
	if (Category == "recent") return QuarkRecentList();
	if (Category == "continue") return QuarkContinueList();
	return QuarkDirectoryList(PathToken, page, true);
}
