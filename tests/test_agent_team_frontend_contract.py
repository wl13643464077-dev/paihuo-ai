"""Regression contracts for collaborative-team terminal and retry states."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source: str, name: str) -> str:
    markers = (f"function {name}(", f"async function {name}(")
    starts = [source.find(marker) for marker in markers]
    starts = [position for position in starts if position >= 0]
    if not starts:
        raise AssertionError(f"missing JavaScript function: {name}")
    start = min(starts)
    candidates = [
        position
        for position in (
            source.find("\nfunction ", start + 1),
            source.find("\nasync function ", start + 1),
        )
        if position >= 0
    ]
    return source[start : min(candidates) if candidates else len(source)]


class AgentTeamFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def _assert_node_ok(self, script: str):
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"node regression failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_failed_standalone_revision_uses_failure_semantics_and_typed_reason(self):
        script = (
            '"use strict";\n'
            'const esc=value=>String(value??"");\n'
            'const cp=value=>JSON.stringify(value);\n'
            'const employeeCanContinue=()=>true;\n'
            'const normalizeDecisionEvidenceRequirements=()=>[];\n'
            'const decisionEvidenceChecklist=()=>"";\n'
            + function(self.source, "taskRevisionPanel")
            + "\n"
            + """
            const base={
              id:70,status:"failed",revision_no:1,brief:{},
              thread:{status:"standalone",revision_count:1,current_task_id:70,
                can_continue:false,can_accept:false,revisions:[]}
            };
            const fallback=taskRevisionPanel(base,"spec");
            if(!fallback.includes("第 1 轮生成未成功")) throw new Error(fallback);
            if(fallback.includes("第 1 轮已交付")) throw new Error(fallback);
            if(fallback.includes("当前任务状态更新中")) throw new Error(fallback);
            const typed=taskRevisionPanel({...base,thread:{...base.thread,
              reason_code:"no_delivered_revision"}},"spec");
            if(!typed.includes("没有可恢复的已交付版本")) throw new Error(typed);
            """
        )
        self._assert_node_ok(script)

    def test_team_progress_distinguishes_failed_active_and_terminal_counts(self):
        script = (
            '"use strict";\n'
            'const esc=value=>String(value??"");\n'
            + function(self.source, "agentTeamProgressHtml")
            + "\n"
            + """
            const mixed=agentTeamProgressHtml({dispatched:[
              {tid:70,name:"A",role:"队长",status:"failed"},
              {tid:71,name:"B",role:"协同",status:"failed"},
              {tid:72,name:"C",role:"策划",status:"failed"},
              {tid:73,name:"D",role:"执行",status:"running"}
            ]});
            if(!mixed.includes("已交付 0/4")) throw new Error(mixed);
            if(!mixed.includes("失败 3")) throw new Error(mixed);
            if(!mixed.includes("进行中 1")) throw new Error(mixed);
            if(mixed.includes("小队进行中 0/4")) throw new Error(mixed);

            const terminal=agentTeamProgressHtml({dispatched:[
              {tid:70,name:"A",role:"队长",status:"failed"},
              {tid:71,name:"B",role:"协同",status:"failed"},
              {tid:72,name:"C",role:"策划",status:"failed"},
              {tid:73,name:"D",role:"执行",status:"failed"}
            ]});
            if(!terminal.includes("本轮已结束")) throw new Error(terminal);
            if(!terminal.includes("失败 4")) throw new Error(terminal);
            if(!terminal.includes("免费重试")) throw new Error(terminal);
            if(terminal.includes("小队进行中")) throw new Error(terminal);
            """
        )
        self._assert_node_ok(script)

    def test_same_task_retry_reenters_polling_and_can_finish_team(self):
        retry_helper = function(self.source, "agentTeamMarkTaskRetrying")
        retry_expert = function(self.source, "retryExpertTask")
        retry_center = function(self.source, "tcRetry")
        self.assertIn("agentTeamMarkTaskRetrying(id)", retry_expert)
        self.assertIn("agentTeamMarkTaskRetrying(id)", retry_center)

        script = (
            '"use strict";\n'
            'let stored={collapsed:false,dispatched:[{tid:70,status:"failed"}]};\n'
            'let summaryStarted=false,rendered=false;\n'
            'const ME={id:1};\n'
            'const document={querySelector:()=>null};\n'
            'const esc=value=>String(value??"");\n'
            'const agentTeamState=()=>stored;\n'
            'const agentTeamPatch=mutate=>mutate(stored);\n'
            'const agentTeamFloatRender=()=>{rendered=true;};\n'
            'const agentTeamSummarize=async auto=>{summaryStarted=auto===true;};\n'
            'const api=async path=>({id:70,status:"done"});\n'
            + retry_helper
            + "\n"
            + function(self.source, "agentTeamPollTick")
            + "\n"
            + function(self.source, "agentTeamProgressHtml")
            + "\n"
            + """
            (async()=>{
              agentTeamMarkTaskRetrying(70);
              if(stored.dispatched[0].status!=="queued") throw new Error(JSON.stringify(stored));
              await agentTeamPollTick();
              if(stored.dispatched[0].status!=="done") throw new Error(JSON.stringify(stored));
              if(summaryStarted) throw new Error("legacy team paid summary started automatically");
              const panel=agentTeamProgressHtml(stored);
              if(!panel.includes("让队长收尾汇总")) throw new Error(panel);
              if(!rendered) throw new Error("team panel was not refreshed");
            })().catch(error=>{console.error(error);process.exitCode=1;});
            """
        )
        self._assert_node_ok(script)

    def test_manual_summary_preserves_a_collapsed_team_panel(self):
        script = (
            '"use strict";\n'
            'let AGENT_TEAM_SUMMARIZING=false;\n'
            'let EXP_LAST_SERVER_RUN_ID=0;\n'
            'let renderedExpanded=null;\n'
            'const stored={collapsed:true,query:"q",summaryTaskId:null,\n'
            '  dispatched:[{tid:70,status:"done"}],\n'
            '  team:{teamName:"测试小队",members:[{idx:1,roleInTeam:"队长"}]}};\n'
            'const agentTeamState=()=>stored;\n'
            'const agentTeamPatch=mutate=>mutate(stored);\n'
            'const agentTeamFloatRender=expanded=>{renderedExpanded=expanded;};\n'
            'const persistentMutationRequestKey=()=>"summary-key";\n'
            'const clearPersistentMutationRequestKey=()=>{};\n'
            'const api=async()=>({task_id:99});\n'
            'const toast=()=>{};\n'
            + function(self.source, "agentTeamSummarize")
            + "\n"
            + """
            (async()=>{
              await agentTeamSummarize(false);
              if(stored.summaryTaskId!==99) throw new Error(JSON.stringify(stored));
              if(renderedExpanded!==false){
                throw new Error(`collapsed panel rendered as ${renderedExpanded}`);
              }
            })().catch(error=>{console.error(error);process.exitCode=1;});
            """
        )
        self._assert_node_ok(script)


if __name__ == "__main__":
    unittest.main()
