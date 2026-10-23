/**
 * 법인WM AI 레이더 — 접속 기록용 구글 Apps Script
 *
 * 설치 (한 번만)
 *  1. 구글 시트 새로 만들기 → 메뉴 [확장 프로그램] → [Apps Script]
 *  2. 이 파일 내용을 전부 붙여넣고 저장
 *  3. 위쪽 함수 선택에서 setup 을 고르고 [실행] → 권한 허용 (시트에 '접속기록', '요약' 탭이 생김)
 *  4. [배포] → [새 배포] → 유형 '웹 앱'
 *       - 다음 사용자 인증 정보로 실행: 나
 *       - 액세스 권한이 있는 사용자: 모든 사용자
 *     → [배포] 후 나오는 '웹 앱 URL'(https://script.google.com/macros/s/.../exec)을 복사
 *  5. GitHub 저장소 Settings → Secrets → Actions 에 VISIT_LOG_URL 로 그 주소를 저장
 *
 * 사이트는 접속할 때마다 (시각, 아이디, PC/모바일, 화면) 한 줄을 보냅니다.
 * '요약' 탭에서 아이디별 접속 횟수·최근 접속을 볼 수 있습니다.
 */
function doPost(e) {
  var d = {};
  try { d = JSON.parse(e.postData.contents); } catch (err) { return ContentService.createTextOutput("bad"); }
  var sh = sheet_("접속기록");
  sh.appendRow([new Date(), String(d.user || "").slice(0, 40), String(d.device || "").slice(0, 10), String(d.page || "").slice(0, 30)]);
  return ContentService.createTextOutput("ok");
}

function setup() {
  var log = sheet_("접속기록");
  if (log.getLastRow() === 0) log.appendRow(["시각", "아이디", "기기", "화면"]);
  log.setFrozenRows(1);
  var sum = sheet_("요약");
  sum.clear();
  sum.getRange("A1").setFormula(
    '=QUERY(접속기록!A:B, "select B, count(A), max(A) where B is not null group by B order by count(A) desc ' +
    'label B \'아이디\', count(A) \'접속 횟수\', max(A) \'최근 접속\'", 1)');
  sum.getRange("E1").setValue("최근 7일");
  sum.getRange("E2").setFormula(
    '=QUERY(접속기록!A:B, "select B, count(A) where A >= date \'"&TEXT(TODAY()-7,"yyyy-mm-dd")&"\' and B is not null group by B order by count(A) desc label B \'아이디\', count(A) \'7일 접속\'", 1)');
}

function sheet_(name) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}
