using System.Text.Json;
using System.Security.Cryptography;
using System.Text;
namespace AgentRadar.Core;
public sealed class CareGate {
 readonly string _directory;readonly object _gate=new();
 public CareGate(string directory){_directory=directory;}
 public bool IsCorrelated(RadarSettings s,SessionRecord r){try{return r.AppId=="openclaw"&&StagePolicy.OpenClawId.IsMatch(r.Id)&&!StagePolicy.IsBlockedOpenClawKey(r.NavigationKey)&&s.CareIds.Contains(r.Id)&&new SettingsRepository(_directory).ReadCareIds().Contains(r.Id);}catch{return false;}}
 static string Key(string id)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(id))).ToLowerInvariant();
 public int Used(string id,double now){lock(_gate){try{return Load().GetValueOrDefault(Key(id),[]).Count(t=>t>now||now-t<3600);}catch{return 3;}}}
 public bool BudgetOpen(string id,double now)=>Used(id,now)<3;
 public void Record(string id,double now){lock(_gate){var all=Load();var key=Key(id);var stamps=all.GetValueOrDefault(key,[]).Where(t=>t>now||now-t<3600).TakeLast(2).ToList();stamps.Add(now);all[key]=stamps;var kept=all.OrderByDescending(p=>p.Value.LastOrDefault()).Take(40).ToDictionary();AtomicFile.Write(_directory,"openclaw-care-budget.json",JsonSerializer.SerializeToUtf8Bytes(new{version=1,attempts=kept}));}}
 Dictionary<string,List<double>> Load(){var path=Path.Combine(_directory,"openclaw-care-budget.json");if(!File.Exists(path))return [];if(new FileInfo(path).Length>65536||File.ResolveLinkTarget(path,false)!=null)throw new IOException("care state invalid");using var doc=JsonDocument.Parse(File.ReadAllBytes(path));var root=doc.RootElement;if(root.GetProperty("version").GetInt32()!=1)throw new IOException("care state invalid");var data=root.GetProperty("attempts");var result=new Dictionary<string,List<double>>();foreach(var p in data.EnumerateObject()){if(result.Count>=40||p.Name.Length!=64||p.Name.Any(c=>!Uri.IsHexDigit(c))||p.Value.ValueKind!=JsonValueKind.Array||p.Value.GetArrayLength()>3)throw new IOException("care state invalid");var values=p.Value.EnumerateArray().Select(x=>x.GetDouble()).ToList();if(values.Any(x=>!double.IsFinite(x)||x<=0))throw new IOException("care state invalid");result[p.Name]=values;}return result;}
}
