module.exports = {
  devtool: 'source-map',
  module: { rules: [
    { test: /\.tsx?$/, exclude: /node_modules/, use: 'ts-loader' },
    { test: /\.css$/, use: ['style-loader', 'css-loader'] },
  ] },
  resolve: { extensions: ['.js', '.ts', '.tsx'] },
};
