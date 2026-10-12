using System;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
namespace AgentRadar.Windows.Ui;
/// WPF's standard multiline editor omits ValuePattern. Expose the complete
/// editable value as well as its inherited TextPattern for assistive clients.
internal sealed class AccessibleTextBox:TextBox {
 protected override AutomationPeer OnCreateAutomationPeer()=>new EditorPeer(this);
 sealed class EditorPeer(AccessibleTextBox editor):TextBoxAutomationPeer(editor),IValueProvider {
  public override object? GetPattern(PatternInterface pattern)=>pattern==PatternInterface.Value?this:base.GetPattern(pattern);
  bool IValueProvider.IsReadOnly=>editor.Dispatcher.Invoke(()=>editor.IsReadOnly);
  string IValueProvider.Value=>editor.Dispatcher.Invoke(()=>editor.Text);
  void IValueProvider.SetValue(string value)=>editor.Dispatcher.Invoke(()=>{if(!editor.IsEnabled)throw new ElementNotEnabledException();if(editor.IsReadOnly)throw new InvalidOperationException("read only");editor.Text=value;});
 }
}
