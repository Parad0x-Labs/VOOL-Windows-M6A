namespace Vool.WindowsHost;

internal sealed class ApprovalDialog : Form
{
    internal ApprovalDialog(string path, ReviewRequest request)
    {
        Text = "VOOL Native Approval";
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(640, 480);
        Size = new Size(780, 610);
        FormBorderStyle = FormBorderStyle.Sizable;

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(18),
            RowCount = 3,
            ColumnCount = 1,
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var heading = new Label
        {
            Dock = DockStyle.Fill,
            AutoSize = true,
            Text = $"{request.Title}\nPOST {path}\nReview every field below. VOOL will sign only this exact request.",
        };
        var details = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Both,
            WordWrap = false,
            Font = new Font(FontFamily.GenericMonospace, 10),
            Text = request.Details,
        };
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
        };
        buttons.Controls.Add(new Button { Text = "Deny", DialogResult = DialogResult.Cancel, AutoSize = true });
        buttons.Controls.Add(new Button { Text = "Approve", DialogResult = DialogResult.OK, AutoSize = true });
        CancelButton = (Button)buttons.Controls[0];

        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(details, 0, 1);
        layout.Controls.Add(buttons, 0, 2);
        Controls.Add(layout);
    }
}
