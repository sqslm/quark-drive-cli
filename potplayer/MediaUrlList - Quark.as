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
	继续观看（追剧中心）。收藏/取消收藏/清空最近、剧集观看状态都是列表内的导航动作行，靠
	PathToken 进入幂等动作页，动作行不是媒体地址；目录收藏状态由桥接的本地收藏
	列表判断。目录首屏会读取该目录的观看状态，给出“继续观看”与“剧集观看状态”
	两行；观看状态只保存在桥接的本地历史里，不上传任何数据。

	0.8.0：继续观看分类的根页改为本地 /v1/watch-center 的分组列表（剧集分组 + 根/
	未归类单集），单独播放的视频在最近观看与追剧中心里都多一条“操作”文件夹行，
	可打开精彩标记窗口或先确认再删除该条观看记录。追剧中心根页只读本地接口，
	分组页进入时才远程读取一次该目录的 /v1/series。
*/

// string GetTitle() 																-> get title for UI
// string GetVersion()																-> get version for manage
// string GetDesc()																	-> get detail information
// array<dictionary> GetCategorys()													-> get category list
// array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)	-> get url list for Category

string QUARK_ACCOUNT_SCOPE = "";
bool QUARK_ACCOUNT_BOUND = false;

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

// 追剧中心（0.8.0）：分组导航标记，以及单个视频的操作标记。
// 操作标记都只带一个文件标识（QuarkValidFid 拒绝“|”），因此记号没有分段歧义：
//   quark-video-actions-<fid>    单集操作页（播放、精彩标记、删除）
//   quark-video-bookmarks-<fid>  打开/复用该视频的精彩标记窗口
//   quark-remove-ask-<fid>       删除确认页（只给导航行，不执行请求）
//   quark-remove-do-<fid>        幂等删除动作页（POST /v1/recent/remove）
string QUARK_CENTER_SERIES = "quark-center-series-";
string QUARK_ACTION_VIDEO = "quark-video-actions-";
string QUARK_ACTION_BOOKMARKS = "quark-video-bookmarks-";
string QUARK_ACTION_HIGHLIGHTS = "quark-highlights-directory-";
string QUARK_HIGHLIGHTS_GROUP = "quark-highlights-group-";
string QUARK_HIGHLIGHTS_CAPTURE = "quark-highlights-capture-";
string QUARK_MARK_PREFIX = "https://quark.invalid/mark/";
string QUARK_ACTION_REMOVE_ASK = "quark-remove-ask-";
string QUARK_ACTION_REMOVE_DO = "quark-remove-do-";

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
	return "0.12.7";
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

	JsonValue account = root["account_id"];
	if (!QUARK_ACCOUNT_BOUND && account.isString()) QUARK_ACCOUNT_SCOPE = account.asString();
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
					{
					string title;
					row.get("title", title);
					row["title"] = title + " · 上次观看：" + last_name.asString();
				}
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

// A successful external window must leave its originating list visible.
array<dictionary> QuarkWindowResult(string message)
{
	array<dictionary> ret = QuarkActionResult(message);
	ret[0]["QuarkRestore"] = "1";
	return ret;
}

array<dictionary> QuarkRestoreOrigin(string Category, string Extra, string origin, string Query, string page)
{
	if (!origin.empty() && origin.find("quark-") != 0 && QuarkValidFid(origin)) return QuarkDirectoryList(origin, page, true);
	return QuarkAccountUrlList(Category, Extra, origin, Query, page);
}

void QuarkAppendMetadataNotice(array<dictionary> &ret, JsonValue &root)
{
	JsonValue notice = root["metadata_notice"];
	if (!notice.isString() || notice.asString().empty()) return;
	dictionary row;
	row["title"] = notice.asString();
	row["folder"] = "1";
	row["PathToken"] = "quark-refresh-view";
	ret.insertLast(row);
}

// 单集观看状态动作记号：目标状态(1=已看/0=未看)、目录 fid、视频 fid 用“|”分隔。
// 三段都不含“|”（QuarkValidFid 拒绝），记号带目标状态，刷新重放同一动作即幂等。
string QuarkWatchToken(bool watched, string parent, string fid)
{
	string state = "0";

	if (watched) state = "1";
	return QUARK_ACTION_WATCH + state + "|" + parent + "|" + fid;
}

// 单集操作文件夹行：导航行，不是媒体地址；标题沿用列表里已有的装饰标题。
void QuarkAppendVideoActions(array<dictionary> &ret, string fid, string title)
{
	if (!QuarkValidFid(fid)) return;

	dictionary row;

	row["title"] = title;
	row["folder"] = "1";
	row["PathToken"] = QUARK_ACTION_VIDEO + fid;
	ret.insertLast(row);
}

// 追剧中心根页：按本机记录的真实父目录分组，补缺失祖先但不扫描目录；进入后才
// 读取一次该目录的 /v1/series；根目录或未归类视频作为单集直接给播放与操作行。
array<dictionary> QuarkWatchCenterList()
{
	array<dictionary> ret;
	string response = QuarkRequest("/v1/watch-center", "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的追剧中心");

	QuarkAppendMetadataNotice(ret, root);
	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("暂无可继续观看的视频：播放一段时间后会出现在这里");
	dictionary manage;
	manage["title"] = "观看管理";
	manage["folder"] = "1";
	manage["PathToken"] = "quark-recent-manage";
	ret.insertLast(manage);

	for (int index = 0; index < items.size(); index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue kind = item["kind"];
		JsonValue key = item["key"];
		JsonValue name = item["name"];
		JsonValue display = item["display"];

		if (!key.isString() || !QuarkValidFid(key.asString())) continue;

		string title = name.isString() ? name.asString() : "";

		if (display.isString() && !display.asString().empty()) title = display.asString();
		if (title.empty()) title = key.asString();

		if (kind.isString() && kind.asString() == "series")
		{
			JsonValue count = item["unfinished_count"];

			if (count.isInt() && count.asInt() > 0)
				title = title + " · 未看完 " + formatInt(count.asInt()) + " 集";

			dictionary row;

			row["title"] = title;
			row["folder"] = "1";
			row["PathToken"] = QUARK_CENTER_SERIES + key.asString();
			ret.insertLast(row);
		}
		else
		{
			// 单集（根目录或未归类）：直接播放；操作集中至观看管理。
			dictionary play;

			play["title"] = title;
			play["url"] = QUARK_PLAY_PREFIX + key.asString();
			ret.insertLast(play);


		}
	}
	return ret;
}

// 目录首屏的“继续观看”和“剧集观看状态”两行。只读取所选目录一次；读取失败时不
// 影响目录本身浏览，静默跳过这两行。全部看完时给一条可导航的完成状态行，避免播放器
// 过滤掉只有标题的行、也避免从头重播整季。
void QuarkAppendSeriesRows(array<dictionary> &ret, string parent, JsonValue &root)
{
	if (!QuarkValidFid(parent)) return;
	if (!root.isObject()) return;

	JsonValue total = root["total"];
	JsonValue watched = root["watched"];

	// 空目录或只有非视频文件时没有剧集：不显示继续/完成/管理行。
	if (total.isInt() && total.asInt() == 0) return;
	if (!total.isInt() || total.asInt() < 1) return;
	dictionary marks;
	marks["title"] = "精彩标记";
	marks["folder"] = "1";
	marks["PathToken"] = QUARK_ACTION_HIGHLIGHTS + parent;
	ret.insertLast(marks);
	dictionary capture;
	capture["title"] = "标记当前时间点";
	capture["folder"] = "1";
	capture["PathToken"] = QUARK_HIGHLIGHTS_CAPTURE + parent;
	ret.insertLast(capture);

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

			done["title"] = "本目录已全部看完 · 剧集管理";
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

	manage["title"] = "剧集管理" + counts;
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

	string request = "/v1/files?parent=" + HostUrlEncode(parent) + "&page=" + HostUrlEncode(page);
	// 我的网盘根目录保持分页，不为首屏扫描整份网盘；非根目录才合并剧集状态。
	if (!PathToken.empty() && PathToken != "0" && page == "1") request += "&include_series=1";
	string response = QuarkRequest(request, "");
	string message;

	if (QuarkFailed(response, message)) return QuarkDirectoryFailure(PathToken, page, message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject())
		return QuarkDirectoryFailure(PathToken, page, "本地网盘组件返回了无法解析的目录");

	bool first = page == "1";

	if (!PathToken.empty() && PathToken != "0" && first)
	{
		dictionary up;

		up["title"] = "..";
		up["folder"] = "1";
		JsonValue ancestor = root["up"];
		if (ancestor.isString()) up["PathToken"] = ancestor.asString();
		else
		{
			up["title"] = "上级目录信息暂不可用 · 刷新重试";
			up["PathToken"] = PathToken;
		}
		ret.insertLast(up);

		JsonValue favorite = root["favorite"];
		if (favorite_action && favorite.isBool()) QuarkAppendFavoriteAction(ret, PathToken, favorite.asBool());
	}
	if (first)
	{
		JsonValue series = root["series"];
		QuarkAppendSeriesRows(ret, parent, series);
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

	dictionary settings;
	settings["title"] = "片头片尾设置";
	settings["folder"] = "1";
	settings["PathToken"] = "quark-series-settings-" + parent;
	ret.insertLast(settings);

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

		if (marked) row["title"] = name.asString() + " · 已看";
		else row["title"] = name.asString() + " · 未看";
		row["folder"] = "1";
		string flag = marked ? "0" : "1";
		row["PathToken"] = QUARK_ACTION_VIDEO + fid.asString() + "|" + parent + "|" + flag;
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

// 剧集分组页：进入时远程读取一次 /v1/series。父级行、观看进度摘要（导航到状态
// 管理页，不是假播放地址）、继续观看与“下一集”播放行（标题用真实文件名，避免与
// 剧集行重名分不清）、自然顺序的剧集（每集可播放并附操作文件夹）。没有下一集的
// 非空目录给可导航的“已到最后一集”提示；空目录（total == 0）另给“该目录没有视频”
// 返回行，且绝不误标“已到最后一集”或“已全部看完”。两种情况都不回到开头重播。
array<dictionary> QuarkCenterSeriesPage(string parent)
{
	array<dictionary> ret;

	if (!QuarkValidFid(parent)) return QuarkActionResult("剧集分组缺少目录标识");

	string response = QuarkRequest("/v1/series?parent=" + HostUrlEncode(parent), "");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkActionResult("本地网盘组件返回了无法解析的剧集状态");

	dictionary up;

	up["title"] = "..";
	up["folder"] = "parent";
	ret.insertLast(up);

	JsonValue total = root["total"];
	JsonValue watched = root["watched"];
	JsonValue complete = root["complete"];

	// 空目录（或只有非视频文件）不是“看完”，也没有“下一集”：只给一条可导航的
	// 返回行，绝不误标“已到最后一集”/“已全部看完”，也不假装能继续观看。
	if (total.isInt() && total.asInt() == 0)
	{
		dictionary empty;

		empty["title"] = "该目录没有视频 · 返回";
		empty["folder"] = "parent";
		ret.insertLast(empty);
		return ret;
	}

	string counts = "";

	if (total.isInt() && watched.isInt())
		counts = "已看 " + formatInt(watched.asInt()) + "/" + formatInt(total.asInt());

	dictionary summary;

	if (complete.isBool() && complete.asBool())
		summary["title"] = "剧集管理 · 已全部看完 · " + counts;
	else
		summary["title"] = "剧集管理 · " + counts;
	summary["folder"] = "1";
	summary["PathToken"] = QUARK_ACTION_SERIES + parent;
	ret.insertLast(summary);

	JsonValue target = root["continue"];

	if (target.isObject())
	{
		JsonValue fid = target["fid"];
		JsonValue display = target["display"];

		if (fid.isString() && QuarkValidFid(fid.asString()))
		{
			dictionary row;

			if (display.isString() && !display.asString().empty()) row["title"] = "继续观看：" + display.asString();
			else row["title"] = "继续观看";
			row["url"] = QUARK_PLAY_PREFIX + fid.asString();
			ret.insertLast(row);
		}
	}

	JsonValue next = root["next"];

	if (next.isObject())
	{
		JsonValue fid = next["fid"];
		JsonValue name = next["name"];

		if (fid.isString() && QuarkValidFid(fid.asString()))
		{
			dictionary row;

			// 显式带“下一集：”前缀，文件名相同的条目也能同剧集行区分开；
			// 地址仍是该下一集的真实稳定播放链接。
			if (name.isString() && !name.asString().empty()) row["title"] = "下一集：" + name.asString();
			else row["title"] = "下一集";
			row["url"] = QUARK_PLAY_PREFIX + fid.asString();
			ret.insertLast(row);
		}
	}
	else
	{
		// 没有下一集：给可导航的返回提示，绝不回到第一集重播。
		dictionary hint;

		hint["title"] = "已到最后一集 · 返回";
		hint["folder"] = "parent";
		ret.insertLast(hint);
	}

	JsonValue episodes = root["episodes"];

	if (episodes.isArray())
	{
		for (int index = 0; index < episodes.size(); index++)
		{
			JsonValue episode = episodes[index];

			if (!episode.isObject()) continue;

			JsonValue fid = episode["fid"];
			JsonValue name = episode["name"];
			JsonValue display = episode["display"];

			if (!fid.isString() || !QuarkValidFid(fid.asString())) continue;

			string title = name.isString() ? name.asString() : "";

			if (display.isString() && !display.asString().empty()) title = display.asString();
			if (title.empty()) continue;

			dictionary row;

			row["title"] = title;
			row["url"] = QUARK_PLAY_PREFIX + fid.asString();
			ret.insertLast(row);

			// 单集操作集中至顶部剧集管理入口。

		}
	}
	return ret;
}

// 单集操作页：本地读取最近记录补上标题（找不到时用通用标题），再给出父级返回行、
// 播放、精彩标记与删除动作。父级行沿用原生文件夹的“..”约定，操作页本身能回到上
// 一级列表，不依赖全局返回。除播放行外都是导航行，没有媒体地址。
array<dictionary> QuarkVideoActionsPage(string fid)
{
	array<dictionary> ret;

	string parent = "";
	string state = "";
	int first = fid.find("|");
	if (first >= 0)
	{
		int second = fid.find("|", first + 1);
		if (second < 0) return QuarkActionResult("视频操作参数无效");
		parent = fid.substr(first + 1, second - first - 1);
		state = fid.substr(second + 1);
		fid = fid.substr(0, first);
		if (!QuarkValidFid(parent) || (state != "0" && state != "1")) return QuarkActionResult("视频操作参数无效");
	}
	if (!QuarkValidFid(fid)) return QuarkActionResult("视频操作缺少文件标识");

	dictionary up;

	up["title"] = "..";
	up["folder"] = "parent";
	ret.insertLast(up);

	string title = "";
	string response = QuarkRequest("/v1/recent", "");
	string message;

	if (!QuarkFailed(response, message))
	{
		JsonReader reader;
		JsonValue root;

		if (reader.parse(response, root) && root.isObject())
		{
			JsonValue items = root["items"];

			if (items.isArray())
			{
				for (int index = 0; index < items.size(); index++)
				{
					JsonValue item = items[index];

					if (!item.isObject()) continue;

					JsonValue value = item["fid"];

					if (!value.isString() || value.asString() != fid) continue;

					JsonValue name = item["name"];
					JsonValue display = item["display"];

					if (display.isString() && !display.asString().empty()) title = display.asString();
					else if (name.isString()) title = name.asString();
					break;
				}
			}
		}
	}
	if (title.empty()) title = "该视频";

	dictionary play;

	play["title"] = "播放：" + title;
	play["url"] = QUARK_PLAY_PREFIX + fid;
	ret.insertLast(play);

	dictionary bookmarks;

	bookmarks["title"] = "精彩标记";
	bookmarks["folder"] = "1";
	bookmarks["PathToken"] = QUARK_ACTION_BOOKMARKS + fid;
	ret.insertLast(bookmarks);

	if (!parent.empty())
	{
		dictionary watched;
		watched["title"] = state == "1" ? "标记为已看" : "标记为未看";
		watched["folder"] = "1";
		watched["PathToken"] = QuarkWatchToken(state == "1", parent, fid);
		ret.insertLast(watched);
	}

	dictionary remove;

	remove["title"] = "删除本条观看记录";
	remove["folder"] = "1";
	remove["PathToken"] = QUARK_ACTION_REMOVE_ASK + fid;
	ret.insertLast(remove);
	return ret;
}

// 精彩标记窗口：本地动作，打开或复用所选视频的管理窗口；未在播放时只可查看、
// 重命名、删除，添加与跳转由服务端要求与当前播放匹配。重复执行只是复用窗口。
array<dictionary> QuarkBookmarksOpenPage(string fid)
{
	if (!QuarkValidFid(fid)) return QuarkActionResult("精彩标记缺少文件标识");

	string response = QuarkRequest("/v1/bookmarks/open", "{\"fid\":\"" + fid + "\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkWindowResult("已打开精彩标记窗口");
}

// 删除确认页：只返回导航行，不执行任何请求。确认行进入幂等删除动作页；
// 移除一行需要显式确认，绝不会误清空整段历史（那是“清空最近观看”的动作）。
array<dictionary> QuarkRecentRemoveAskPage(string fid)
{
	array<dictionary> ret;

	if (!QuarkValidFid(fid)) return QuarkActionResult("删除操作缺少文件标识");

	dictionary up;

	up["title"] = "..";
	up["folder"] = "parent";
	ret.insertLast(up);

	dictionary confirm;

	confirm["title"] = "确认删除本条观看记录（不影响网盘文件）";
	confirm["folder"] = "1";
	confirm["PathToken"] = QUARK_ACTION_REMOVE_DO + fid;
	ret.insertLast(confirm);
	return ret;
}

// 单条记录删除：POST /v1/recent/remove 幂等，只删本地这一条记录与进度，
// 刷新重放同一动作无副作用，也不碰网盘文件或其它记录。
array<dictionary> QuarkRecentRemovePage(string fid)
{
	if (!QuarkValidFid(fid)) return QuarkActionResult("删除操作缺少文件标识");

	string response = QuarkRequest("/v1/recent/remove", "{\"fid\":\"" + fid + "\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkActionResult("已删除本条观看记录");
}

// 诊断入口：请本地服务拉起独立的“插件管理”窗口。窗口自行读取运行时记录，
// 脚本不接触令牌或地址；重复刷新时服务端复用同一个窗口，操作幂等。
array<dictionary> QuarkManagementPage()
{
	string response = QuarkRequest("/v1/management/open", "{\"client\":\"potplayer\"}");
	string message;

	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkActionResult("已打开插件管理窗口");
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

	QuarkAppendMetadataNotice(ret, root);
	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("收藏列表为空：进入目录后选“收藏当前目录”即可添加");

	QuarkAppendItems(ret, items);
	return ret;
}

// 最近观看的视频行：每条的普通播放行保持直接播放（从该集开始的同目录选集），
// 操作集中至顶部观看管理入口。
void QuarkAppendRecentItems(array<dictionary> &ret, JsonValue &items)
{
	if (!items.isArray()) return;

	for (int index = 0; index < items.size(); index++)
	{
		JsonValue item = items[index];

		if (!item.isObject()) continue;

		JsonValue fid = item["fid"];
		JsonValue name = item["name"];
		JsonValue display = item["display"];

		if (!fid.isString() || !name.isString() || !QuarkValidFid(fid.asString())) continue;

		string title = name.asString();

		if (display.isString() && !display.asString().empty()) title = display.asString();

		dictionary play;

		play["title"] = title;
		play["url"] = QUARK_PLAY_PREFIX + fid.asString();
		ret.insertLast(play);


	}
}

// 最近观看：条目直接复用稳定播放链接，从该集开始同目录选集；顶部仅提供一个观看管理入口。管理页集中处理精彩标记和观看记录。
array<dictionary> QuarkRecentList()
{
	array<dictionary> ret;
	string response = QuarkRequest("/v1/recent", "");
	string message;

	if (QuarkFailed(response, message)) return QuarkStatus(message);

	JsonReader reader;
	JsonValue root;

	if (!reader.parse(response, root) || !root.isObject()) return QuarkStatus("本地网盘组件返回了无法解析的最近记录");

	QuarkAppendMetadataNotice(ret, root);
	JsonValue items = root["items"];

	if (!items.isArray() || items.size() == 0) return QuarkStatus("最近没有播放记录：打开一个视频后会出现在这里");

	dictionary clear;

	clear["title"] = "观看管理";
	clear["PathToken"] = "quark-recent-manage";
	clear["folder"] = "1";
	ret.insertLast(clear);

	QuarkAppendRecentItems(ret, items);
	return ret;
}

array<dictionary> QuarkRecentManagePage()
{
	array<dictionary> ret;
	dictionary up;
	up["title"] = "..";
	up["folder"] = "parent";
	ret.insertLast(up);
	dictionary clear;
	clear["title"] = "清空最近观看";
	clear["folder"] = "1";
	clear["PathToken"] = "quark-recent-clear-ask";
	ret.insertLast(clear);
	string response = QuarkRequest("/v1/recent", "");
	string message;
	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	JsonReader reader;
	JsonValue root;
	if (!reader.parse(response, root) || !root.isObject()) return QuarkActionResult("观看记录读取失败");
	QuarkAppendMetadataNotice(ret, root);
	JsonValue items = root["items"];
	if (!items.isArray()) return ret;
	for (int index = 0; index < items.size(); index++)
	{
		JsonValue fid = items[index]["fid"];
		JsonValue name = items[index]["display"];
		if (!name.isString()) name = items[index]["name"];
		if (fid.isString() && name.isString()) QuarkAppendVideoActions(ret, fid.asString(), name.asString());
	}
	return ret;
}

array<dictionary> QuarkHighlightsList(string parent)
{
	array<dictionary> ret;
	dictionary manage;
	manage["title"] = (parent.empty() || parent == "~unknown") ? "管理全部精彩标记" : "管理当前目录精彩标记";
	manage["folder"] = "1";
	manage["PathToken"] = "quark-highlights-manage-all";
	if (!parent.empty() && parent != "~unknown") manage["PathToken"] = QUARK_ACTION_HIGHLIGHTS + parent;
	ret.insertLast(manage);
	string request = "/v1/bookmarks";
	if (parent == "~unknown") request += "?unclassified=1";
	else if (!parent.empty()) request += "?parent=" + HostUrlEncode(parent);
	string response = QuarkRequest(request, "");
	string message;
	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	JsonReader reader;
	JsonValue root;
	if (!reader.parse(response, root) || !root.isObject()) return QuarkActionResult("读取精彩标记失败");
	if (parent.empty())
	{
		JsonValue groups = root["groups"];
		if (!groups.isArray()) return ret;
		for (int index = 0; index < groups.size(); index++)
		{
			JsonValue group = groups[index];
			JsonValue key = group["parent"];
			JsonValue name = group["name"];
			JsonValue count = group["count"];
			if (!key.isString() || !name.isString() || !count.isInt()) continue;
			dictionary folder;
			folder["title"] = name.asString() + "（" + formatInt(count.asInt()) + " 个精彩点）";
			folder["folder"] = "1";
			folder["PathToken"] = QUARK_HIGHLIGHTS_GROUP + (key.asString().empty() ? "~unknown" : key.asString());
			ret.insertLast(folder);
		}
		return ret;
	}
	JsonValue items = root["items"];
	if (!items.isArray()) return ret;
	int count = items.size();
	for (int index = 0; index < count; index++)
	{
		JsonValue item = items[index];
		JsonValue id = item["id"];
		JsonValue name = item["name"];
		JsonValue position = item["position_ms"];
		JsonValue note = item["note"];
		if (!id.isString() || !QuarkValidFid(id.asString()) || !name.isString() || !position.isInt()) continue;
		int seconds = position.asInt() / 1000;
		string minute = formatInt((seconds / 60) % 60);
		string second = formatInt(seconds % 60);
		if (minute.length() < 2) minute = "0" + minute;
		if (second.length() < 2) second = "0" + second;
		dictionary row;
		string title = name.asString() + " · " + formatInt(seconds / 3600) + ":" + minute + ":" + second;
		if (note.isString() && !note.asString().empty()) title += " · " + note.asString();
		row["title"] = title;
		row["url"] = QUARK_MARK_PREFIX + id.asString();
		ret.insertLast(row);
	}
	return ret;
}

array<dictionary> QuarkHighlightsOpenPage(string parent, bool capture)
{
	if (!QuarkValidFid(parent)) return QuarkActionResult("目录标识无效");
	string body = "{\"parent\":\"" + parent + "\"";
	if (capture) body += ",\"action\":\"add\"";
	body += "}";
	string response = QuarkRequest("/v1/bookmarks/open", body);
	string message;
	if (QuarkFailed(response, message)) return QuarkActionResult(message);
	return QuarkWindowResult(capture ? "请在精彩标记窗口填写备注" : "已打开当前目录精彩标记窗口");
}

const string QUARK_ROOT_CATEGORY = "";

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

	resume["title"] = "{$CP936=追剧中心$}{$CP950=追劇中心$}{$CP0=Continue Watching$}";
	resume["Category"] = "continue";
	ret.insertLast(resume);
	dictionary highlights;
	highlights["title"] = "{$CP936=精彩标记$}{$CP950=精彩標記$}{$CP0=Highlights$}";
	highlights["Category"] = "highlights";
	ret.insertLast(highlights);

	dictionary diagnostics;

	diagnostics["title"] = "{$CP936=插件管理$}{$CP950=插件管理$}{$CP0=Plugin Manager$}";
	diagnostics["Category"] = "diagnostics";
	ret.insertLast(diagnostics);
	if (!QUARK_ROOT_CATEGORY.empty())
	{
		array<dictionary> selected;
		for (int index = 0; index < ret.size(); index++)
		{
			string category;
			ret[index].get("Category", category);
			if (category == QUARK_ROOT_CATEGORY) selected.insertLast(ret[index]);
		}
		return selected;
	}
	return ret;
}

array<dictionary> QuarkAccountUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)
{
	string page = PageToken;

	if (page.empty()) page = "1";

	// 诊断窗口是纯本地功能：不需要夸克登录，必须在登录检查之前处理，
	// 否则未登录用户永远看不到诊断入口。
	if (Category == "diagnostics") return QuarkManagementPage();
	if (PathToken == "quark-recent-manage") return QuarkRecentManagePage();
	if (PathToken == "quark-recent-clear-ask")
	{
		array<dictionary> ret = QuarkActionResult("取消清空");
		dictionary confirm;
		confirm["title"] = "确认清空全部观看记录";
		confirm["folder"] = "1";
		confirm["PathToken"] = QUARK_ACTION_CLEAR_RECENT;
		ret.insertLast(confirm);
		return ret;
	}
	if (PathToken.find("quark-series-settings-") == 0)
	{
		string parent = PathToken.substr(22);
		if (!QuarkValidFid(parent)) return QuarkActionResult("剧集目录无效");
		string response = QuarkRequest("/v1/series-settings/open", "{\"parent\":\"" + parent + "\"}");
		string message;
		if (QuarkFailed(response, message)) return QuarkActionResult(message);
		return QuarkWindowResult("已打开片头片尾设置窗口");
	}

	if (PathToken == "quark-highlights-manage-all")
	{
		string response = QuarkRequest("/v1/bookmarks/open", "{\"scope\":\"all\"}");
		string message;
		if (QuarkFailed(response, message)) return QuarkActionResult(message);
		return QuarkWindowResult("已打开全部精彩标记管理窗口");
	}
	if (Category == "highlights" && PathToken.empty()) return QuarkHighlightsList("");
	if (PathToken.find(QUARK_HIGHLIGHTS_GROUP) == 0)
		return QuarkHighlightsList(PathToken.substr(QUARK_HIGHLIGHTS_GROUP.length()));
	if (PathToken.find(QUARK_HIGHLIGHTS_CAPTURE) == 0)
		return QuarkHighlightsOpenPage(PathToken.substr(QUARK_HIGHLIGHTS_CAPTURE.length()), true);
	if (PathToken.find(QUARK_ACTION_HIGHLIGHTS) == 0)
		return QuarkHighlightsOpenPage(PathToken.substr(QUARK_ACTION_HIGHLIGHTS.length()), false);

	// 追剧中心以本地记录为基础：登出或冷却时仍能看到分组列表。
	if (Category == "continue" && PathToken.empty()) return QuarkWatchCenterList();

	// 单集操作、精彩标记窗口与删除记录都是本地动作：同样在登录检查之前处理，
	// 这样登出或冷却时仍能管理已有记录。删除必须先经过确认页（REMOVE_ASK），
	// 只有确认行携带的 REMOVE_DO 才会真正发请求。
	if (PathToken.find(QUARK_ACTION_REMOVE_DO) == 0)
		return QuarkRecentRemovePage(PathToken.substr(QUARK_ACTION_REMOVE_DO.length()));

	if (PathToken.find(QUARK_ACTION_REMOVE_ASK) == 0)
		return QuarkRecentRemoveAskPage(PathToken.substr(QUARK_ACTION_REMOVE_ASK.length()));

	if (PathToken.find(QUARK_ACTION_VIDEO) == 0)
		return QuarkVideoActionsPage(PathToken.substr(QUARK_ACTION_VIDEO.length()));

	if (PathToken.find(QUARK_ACTION_BOOKMARKS) == 0)
		return QuarkBookmarksOpenPage(PathToken.substr(QUARK_ACTION_BOOKMARKS.length()));

	if (!QuarkEnsureLogin()) return QuarkStatus(QuarkLoginReason());

	// 剧集分组页会远程读取一次该目录的 /v1/series，因此需要登录。
	if (PathToken.find(QUARK_CENTER_SERIES) == 0)
		return QuarkCenterSeriesPage(PathToken.substr(QUARK_CENTER_SERIES.length()));

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

	if (!PathToken.empty() && QuarkValidFid(PathToken)) return QuarkDirectoryList(PathToken, page, true);
	if (Category == "search") return QuarkSearchList(Query, PathToken, page);
	if (Category == "favorites") return QuarkFavoritesList(PathToken, page);
	if (Category == "recent") return QuarkRecentList();
	if (Category == "continue") return QuarkWatchCenterList();
	return QuarkDirectoryList(PathToken, page, true);
}

bool QuarkValidAccount(string value)
{
    if (value.length() != 64) return false;
    string hex = "0123456789abcdef";
    for (int index = 0; index < value.length(); index++)
        if (hex.find(value.substr(index, 1)) < 0) return false;
    return true;
}

// Keep the originating account on every navigation token and media link.
array<dictionary> GetUrlList(string Category, string Extra, string PathToken, string Query, string PageToken)
{
    QUARK_ACCOUNT_SCOPE = "";
    QUARK_ACCOUNT_BOUND = false;
    string tag = "quark-account:";
    if (PathToken.find(tag) == 0)
    {
        int split = PathToken.find("|");
        if (split < 0) return QuarkStatus("账号标识无效，请从根目录重新打开");
        QUARK_ACCOUNT_SCOPE = PathToken.substr(tag.length(), split - tag.length());
        PathToken = PathToken.substr(split + 1);
        QUARK_ACCOUNT_BOUND = true;
    }
    if (PageToken.find(tag) == 0)
    {
        int split = PageToken.find("|");
        if (split < 0) return QuarkStatus("分页标识无效，请从根目录重新打开");
        string scope = PageToken.substr(tag.length(), split - tag.length());
        if (QUARK_ACCOUNT_BOUND && scope != QUARK_ACCOUNT_SCOPE)
            return QuarkStatus("账号已切换，请从根目录重新打开");
        QUARK_ACCOUNT_SCOPE = scope;
        QUARK_ACCOUNT_BOUND = true;
        PageToken = PageToken.substr(split + 1);
    }
    if (QUARK_ACCOUNT_BOUND && !QuarkValidAccount(QUARK_ACCOUNT_SCOPE))
        return QuarkStatus("账号标识无效，请从根目录重新打开");
    if (!QUARK_ACCOUNT_BOUND && !PathToken.empty())
        return QuarkStatus("旧目录已失效，请从根目录重新打开");
    if (!QUARK_ACCOUNT_BOUND) QuarkAuthenticated();
    // Origin travels with the action, through every management submenu.
    // It is inside the account envelope: another account cannot reuse it.
    string origin = PathToken;
    string originPage = PageToken.empty() ? "1" : PageToken;
    string navigation = "quark-origin:";
    if (PathToken.find(navigation) == 0)
    {
        int first = PathToken.find("|", navigation.length());
        int second = PathToken.find("|", first + 1);
        if (first < 0 || second < 0) return QuarkStatus("导航来源无效，请重新打开所在目录");
        origin = HostUrlDecode(PathToken.substr(navigation.length(), first - navigation.length()));
        originPage = HostUrlDecode(PathToken.substr(first + 1, second - first - 1));
        PathToken = HostUrlDecode(PathToken.substr(second + 1));
    }
    else if (PathToken.find("quark-series-settings-") == 0)
        origin = PathToken.substr(22);
    else if (PathToken.find(QUARK_ACTION_HIGHLIGHTS) == 0)
        origin = PathToken.substr(QUARK_ACTION_HIGHLIGHTS.length());
    else if (PathToken.find(QUARK_HIGHLIGHTS_CAPTURE) == 0)
        origin = PathToken.substr(QUARK_HIGHLIGHTS_CAPTURE.length());
    else if (PathToken.find(QUARK_ACTION_SERIES) == 0)
        origin = PathToken.substr(QUARK_ACTION_SERIES.length());
    else if (PathToken.find("quark-") == 0)
        origin = "";
    if (PageToken.find("quark-page:") == 0)
    {
        int split = PageToken.find("|", 11);
        if (split < 0) return QuarkStatus("分页来源无效，请重新打开所在目录");
        PathToken = HostUrlDecode(PageToken.substr(11, split - 11));
        PageToken = PageToken.substr(split + 1);
        origin = PathToken;
        originPage = PageToken;
    }
    if (PathToken.find(QUARK_CENTER_SERIES) == 0)
        origin = PathToken.substr(QUARK_CENTER_SERIES.length());
    if (PathToken.find(QUARK_HIGHLIGHTS_GROUP) == 0)
        origin = PathToken;
    if (PathToken == "quark-back" || PathToken == "quark-home")
    {
        PathToken = origin;
        PageToken = originPage;
    }
    if (PathToken == "quark-refresh-view")
    {
        PathToken = origin;
        PageToken = originPage;
    }
    array<dictionary> ret = QuarkAccountUrlList(Category, Extra, PathToken, Query, PageToken);
    string restore;
    if (ret.size() > 0 && ret[0].get("QuarkRestore", restore))
    {
        ret = QuarkRestoreOrigin(Category, Extra, origin, Query, originPage);
        PathToken = origin;
        PageToken = originPage;
    }
    if (QUARK_ACCOUNT_SCOPE.empty()) return ret;
    for (int index = 0; index < ret.size(); index++)
    {
        string value;
        string folder;
        if (Category != "diagnostics" && ret[index].get("folder", folder) && folder == "parent")
        {
            ret[index]["folder"] = "1";
            ret[index]["PathToken"] = "quark-back";
        }
        if (ret[index].get("PathToken", value))
        {
            if (value.find("quark-") == 0)
                value = navigation + HostUrlEncode(origin) + "|" + HostUrlEncode(originPage) + "|" + HostUrlEncode(value);
            ret[index]["PathToken"] = tag + QUARK_ACCOUNT_SCOPE + "|" + value;
        }
        if (ret[index].get("PageToken", value))
            ret[index]["PageToken"] = tag + QUARK_ACCOUNT_SCOPE + "|quark-page:" + HostUrlEncode(PathToken) + "|" + value;
        if (ret[index].get("url", value) && value.find("https://quark.invalid/") == 0)
            ret[index]["url"] = value + "?account=" + QUARK_ACCOUNT_SCOPE;
    }
    return ret;
}
