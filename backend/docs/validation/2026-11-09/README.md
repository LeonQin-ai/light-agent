# light-agent issue 验证

| Issue | 验证 | 截图 |
| --- | --- | --- |
| #256 公开阶段进展 | 浏览器验证执行中与最终回复保留阶段说明。另用真实模型在同一会话先写发布记录，再调查合成重启日志。第二项只处理当前任务，引用正确的证据编号，留下 7 条不同的公开阶段说明。 | [桌面](issue256-1280.png) · [窄屏](issue256-390.png) |
| #257 重启恢复 | 运行中强制结束隔离服务，重启后从聊天界面点击恢复，验证原始输入重放和产物内容。模型响应由测试服务提供。 | [中断后](issue257-interrupted.png) · [恢复完成](issue257-completed.png) |
| #258 模型温度 | 输入框底部取代原深度思考入口。浏览器滑块设为 0.65，确认提交携带该值。独立 HTTP 回归覆盖默认路由和两个并行会话，并在模型接口检查 0、0.5 和 1。 | [新版输入框](issue258-composer.png) |
| #259 图片内嵌 | 真实模型读取五张本地测试图，确认图片逐项内嵌、顺序与像素正确，重启后仍可预览，尾部无重复。桌面和窄屏断言均通过。 | [初次显示](issue259-inline.png) · [重启后](issue259-restarted.png) |
| #260 Windows 悬浮窗 | 通过真实调度 API 验证空闲和运行两个状态。新版是透明无边框机器人，原生验证程序检查窗口可见、透明色键、无标题栏、Logo 绘制和关闭。新版图来自原生绘制缓冲，透明色键转换为 PNG alpha。 | [新版机器人](issue260-robot.png) · [原版空闲](issue260-idle.jpg) · [原版运行中](issue260-running.jpg) |

图片任务共 3 次模型调用、5 次工具调用，输入 22,842 tokens，输出 1,062 tokens，模型请求失败 0 次。模型接口未返回金额。

同会话任务边界验证两次任务均为 DONE，共 19 次模型调用、13 次工具调用，输入 131,191 tokens，输出 5,979 tokens，模型请求失败 0 次。使用合成日志模拟原报告的场景。

## 全身 Logo 桌宠

使用内置图像生成工具，以仓库 Logo 为参考制作全身透明素材 [light-agent-pet.png](../../../assets/light-agent-pet.png)。Windows 原生测试覆盖逐像素透明合成、重复开启、关闭、从窗口侧关闭和再次开启。浏览器验证按钮状态同步。

生成提示：

> Use case: identity-preserve. Turn the mascot from the reference light-agent logo into one isolated, fully embodied desktop-pet character. Recreate the same chibi blue-violet orb creature as a polished 3D toy render, full body visible from helmet tips down to two tiny rounded boots. Keep its gold-trimmed tall jackal ears, bright cyan forehead gem, luminous cyan-and-violet infinity eye band, two large expressive eyes, pink cheeks, little smile, dark purple gauntlets, gold infinity belt, and its gold crescent staff with blue gem held at its side. The round character itself is the whole pet, with arms and legs; no robot casing, monitor, frame, badge, emblem backing, or image on its face. Front three-quarter view, friendly and energetic. Strong clean silhouette with modeled depth, soft studio lighting and smooth glossy materials, still readable at 150 pixels tall. Center the entire character and staff with modest even padding. Transparent RGBA background, no scenery, no bird, no ground or drop shadow, no extra effects outside the silhouette, no text.
